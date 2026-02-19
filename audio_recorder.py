"""
Audio Recorder — sound-triggered WAV clip recording for dog vocalization detection.

Continuously captures microphone audio via I2S, maintains a rolling pre-buffer
in PSRAM, and starts saving a WAV clip when audio exceeds a volume threshold.
Includes the pre-buffer in the clip so the beginning of a bark isn't clipped.

State machine:
    IDLE → audio exceeds threshold → RECORDING
    RECORDING → silence detected (or max duration) → IDLE
    Each transition writes/finalizes a WAV file on the SD card.

Usage:
    recorder = AudioRecorder(i2c, rtc, log)
    recorder.init()
    while True:
        recorder.poll()   # Call from main loop at ~30 Hz
        print(recorder.current_rms, recorder.state)
"""

import os
import time
import struct
from machine import Pin, I2S

import board_config as BOARD
from drivers.es8311 import ES8311


# ─── States ───────────────────────────────────────────────────────
STATE_IDLE      = 'idle'
STATE_RECORDING = 'recording'


class CircularBuffer:
    """Fixed-size circular byte buffer in PSRAM for audio pre-buffering."""

    __slots__ = ('buf', 'size', 'wr', 'filled')

    def __init__(self, size):
        self.buf = bytearray(size)
        self.size = size
        self.wr = 0
        self.filled = 0

    def write(self, data, length=None):
        """Append data to buffer, overwriting oldest bytes when full."""
        n = length if length is not None else len(data)
        if n <= 0:
            return
        buf = self.buf
        size = self.size
        wr = self.wr

        if n >= size:
            # Data larger than buffer — keep only the tail
            start = n - size
            buf[:] = data[start:start + size]
            self.wr = 0
            self.filled = size
            return

        end = wr + n
        if end <= size:
            buf[wr:end] = data[:n]
        else:
            first = size - wr
            buf[wr:] = data[:first]
            buf[:n - first] = data[first:n]
        self.wr = end % size
        self.filled = min(self.filled + n, size)

    def read_ordered(self):
        """Return all valid data in chronological order as memoryview pair.

        Returns (chunk1, chunk2) — write chunk1 then chunk2 to get
        oldest-to-newest order.  Avoids copying the whole buffer.
        """
        if self.filled < self.size:
            return memoryview(self.buf)[:self.filled], b''
        # Buffer full: data from wr..end is oldest, 0..wr is newest
        return memoryview(self.buf)[self.wr:], memoryview(self.buf)[:self.wr]

    def clear(self):
        self.wr = 0
        self.filled = 0


class AudioRecorder:
    """Sound-triggered WAV recorder with circular pre-buffer."""

    def __init__(self, i2c, rtc=None, log=None,
                 trigger_threshold=None, silence_threshold=None,
                 silence_ms=None, pre_buffer_ms=None,
                 max_clip_sec=None):
        self._i2c = i2c
        self._rtc = rtc
        self._log = log

        # Configurable thresholds
        self.trigger_threshold = trigger_threshold or BOARD.AUDIO_TRIGGER_THRESH
        self.silence_threshold = silence_threshold or BOARD.AUDIO_SILENCE_THRESH
        self._silence_ms = silence_ms or BOARD.AUDIO_SILENCE_MS
        self._max_clip_sec = max_clip_sec or BOARD.AUDIO_MAX_CLIP_SEC

        # Pre-buffer size in bytes: ms * (sample_rate / 1000) * 2 bytes
        pre_ms = pre_buffer_ms or BOARD.AUDIO_PRE_BUFFER_MS
        self._pre_buf_bytes = (pre_ms * BOARD.AUDIO_SAMPLE_RATE // 1000) * 2

        # Runtime state
        self._codec = None
        self._i2s = None
        self._pre_buf = None
        self._read_buf = None

        self._state = STATE_IDLE
        self._current_rms = 0
        self._trigger_count = 0       # Consecutive above-threshold chunks
        self._silence_start = 0       # ticks_ms when silence began
        self._rec_start = 0           # ticks_ms when recording started
        self._rec_samples = 0         # Samples written to current WAV
        self._rec_file = None         # Open file handle
        self._rec_path = None         # Current WAV filepath

        self._clip_count = 0
        self._total_duration = 0.0    # Total seconds recorded this session

        self._paused = False

    # ─── Init / Deinit ────────────────────────────────────────────

    def init(self):
        """Initialize ES8311 codec, I2S peripheral, and pre-buffer."""
        # Ensure clips directory exists
        try:
            os.stat(BOARD.CLIPS_DIR)
        except OSError:
            try:
                os.mkdir(BOARD.CLIPS_DIR)
            except OSError:
                pass

        # Init codec
        self._codec = ES8311(self._i2c)
        self._codec.init()

        # Ensure I2S DOUT pin (speaker) stays low — we only use RX
        Pin(BOARD.I2S_DOUT, Pin.OUT, value=0)

        # Init I2S in receive (mic) mode
        # ibuf=4096 gives ~128ms of DMA buffer at 16kHz mono 16-bit
        self._i2s = I2S(0,
                        sck=Pin(BOARD.I2S_BCLK),
                        ws=Pin(BOARD.I2S_WS),
                        sd=Pin(BOARD.I2S_DIN),
                        mode=I2S.RX,
                        bits=16,
                        format=I2S.MONO,
                        rate=BOARD.AUDIO_SAMPLE_RATE,
                        ibuf=4096)

        # Allocate buffers (large ones go to PSRAM automatically)
        self._pre_buf = CircularBuffer(self._pre_buf_bytes)
        self._read_buf = bytearray(512)  # 16ms at 16 kHz

        self._state = STATE_IDLE
        self._trigger_count = 0
        self._debug_count = 0    # Print first few readings for debug

        # Verify I2S is returning data
        import time as _time
        _time.sleep_ms(100)  # Let DMA fill
        test_buf = bytearray(256)
        test_n = self._i2s.readinto(test_buf)
        test_rms = self._calc_rms(test_buf, test_n) if test_n > 0 else -1
        print(f"AudioRecorder: I2S test read: {test_n} bytes, RMS={test_rms}")

        if self._log:
            self._log.info("AudioRecorder: initialized")

    def deinit(self):
        """Stop recording, release I2S and codec."""
        if self._state == STATE_RECORDING:
            self._stop_recording()
        if self._i2s:
            self._i2s.deinit()
            self._i2s = None
        if self._codec:
            self._codec.deinit()
            self._codec = None

    # ─── Main poll (call from main loop) ──────────────────────────

    def poll(self):
        """Read audio from I2S and drive the recording state machine.

        Call this at ~30 Hz from the main loop.  Non-blocking — reads
        whatever data is available in the I2S DMA buffer.
        """
        if self._paused or self._i2s is None:
            return

        buf = self._read_buf
        n = self._i2s.readinto(buf)
        if n is None or n <= 0:
            return

        # Compute RMS amplitude
        rms = self._calc_rms(buf, n)
        self._current_rms = rms

        # Debug: print first 10 readings
        if self._debug_count < 10:
            print(f"  audio: {n}B rms={rms} thr={self.trigger_threshold}")
            self._debug_count += 1

        if self._state == STATE_IDLE:
            # Feed pre-buffer
            self._pre_buf.write(buf, n)

            # Check for trigger
            if rms >= self.trigger_threshold:
                self._trigger_count += 1
                if self._trigger_count >= 2:
                    self._start_recording()
            else:
                self._trigger_count = 0

        elif self._state == STATE_RECORDING:
            # Write audio to WAV file
            self._write_audio(buf, n)

            # Check for silence
            if rms < self.silence_threshold:
                if self._silence_start == 0:
                    self._silence_start = time.ticks_ms()
                elif time.ticks_diff(time.ticks_ms(), self._silence_start) >= self._silence_ms:
                    self._stop_recording()
                    return
            else:
                self._silence_start = 0

            # Check max duration
            elapsed_ms = time.ticks_diff(time.ticks_ms(), self._rec_start)
            if elapsed_ms >= self._max_clip_sec * 1000:
                self._stop_recording()

    # ─── Recording state transitions ──────────────────────────────

    def _start_recording(self):
        """Open WAV file, dump pre-buffer, switch to RECORDING state."""
        self._rec_path = self._make_filepath()
        try:
            self._rec_file = open(self._rec_path, "wb")
        except OSError as e:
            if self._log:
                self._log.error(f"AudioRecorder: can't open file: {e}")
            self._trigger_count = 0
            return

        # Write WAV header (placeholder — finalized on stop)
        _write_wav_header(self._rec_file, BOARD.AUDIO_SAMPLE_RATE, 0)

        # Dump pre-buffer
        chunk1, chunk2 = self._pre_buf.read_ordered()
        pre_bytes = 0
        if len(chunk1) > 0:
            self._rec_file.write(chunk1)
            pre_bytes += len(chunk1)
        if len(chunk2) > 0:
            self._rec_file.write(chunk2)
            pre_bytes += len(chunk2)
        self._pre_buf.clear()

        self._rec_samples = pre_bytes // 2
        self._rec_start = time.ticks_ms()
        self._silence_start = 0
        self._state = STATE_RECORDING
        self._trigger_count = 0

        if self._log:
            self._log.info(f"AudioRecorder: recording → {self._rec_path}")

    def _write_audio(self, buf, n):
        """Write a chunk of audio data to the open WAV file."""
        if self._rec_file is None:
            return
        try:
            self._rec_file.write(memoryview(buf)[:n])
            self._rec_samples += n // 2
        except OSError as e:
            if self._log:
                self._log.error(f"AudioRecorder: write error: {e}")
            self._stop_recording()

    def _stop_recording(self):
        """Finalize WAV header, close file, update stats."""
        if self._rec_file:
            try:
                self._rec_file.close()
            except Exception:
                pass
            self._rec_file = None

        # Update WAV header with actual sample count
        if self._rec_path and self._rec_samples > 0:
            try:
                _finalize_wav_header(self._rec_path,
                                    BOARD.AUDIO_SAMPLE_RATE,
                                    self._rec_samples)
            except Exception as e:
                if self._log:
                    self._log.error(f"AudioRecorder: finalize error: {e}")

        duration = self._rec_samples / BOARD.AUDIO_SAMPLE_RATE
        self._clip_count += 1
        self._total_duration += duration

        if self._log:
            self._log.info(
                f"AudioRecorder: clip #{self._clip_count} saved "
                f"({duration:.1f}s, {self._rec_samples * 2} bytes)"
            )

        # Reset state
        self._state = STATE_IDLE
        self._rec_samples = 0
        self._rec_path = None
        self._trigger_count = 0
        self._silence_start = 0

        # Free memory
        import gc
        gc.collect()

    # ─── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _calc_rms(buf, n):
        """Calculate integer RMS of 16-bit PCM samples."""
        n_samples = n // 2
        if n_samples == 0:
            return 0
        sum_sq = 0
        for i in range(0, n, 2):
            sample = struct.unpack_from('<h', buf, i)[0]
            sum_sq += sample * sample
        mean_sq = sum_sq // n_samples
        if mean_sq == 0:
            return 0
        # Integer sqrt via Newton's method
        x = mean_sq
        y = (x + 1) // 2
        while y < x:
            x = y
            y = (x + mean_sq // x) // 2
        return x

    def _make_filepath(self):
        """Generate WAV filepath from RTC timestamp."""
        if self._rtc:
            try:
                dt = self._rtc.datetime()
                return "{}/{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.wav".format(
                    BOARD.CLIPS_DIR,
                    dt[0], dt[1], dt[2], dt[4], dt[5], dt[6])
            except Exception:
                pass
        # Fallback: use system ticks
        t = time.localtime()
        return "{}/{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.wav".format(
            BOARD.CLIPS_DIR,
            t[0], t[1], t[2], t[3], t[4], t[5])

    # ─── Public properties for UI ─────────────────────────────────

    @property
    def state(self):
        """Current state: 'idle' or 'recording'."""
        return self._state

    @property
    def is_recording(self):
        return self._state == STATE_RECORDING

    @property
    def current_rms(self):
        """Latest audio RMS level (for VU meter)."""
        return self._current_rms

    @property
    def clip_count(self):
        """Number of clips recorded this session."""
        return self._clip_count

    @property
    def total_duration(self):
        """Total seconds of audio recorded this session."""
        return self._total_duration

    @property
    def current_clip_duration(self):
        """Seconds into current recording (0 if idle)."""
        if self._state != STATE_RECORDING:
            return 0.0
        return time.ticks_diff(time.ticks_ms(), self._rec_start) / 1000.0

    # ─── Settings ─────────────────────────────────────────────────

    def set_threshold(self, value):
        """Adjust trigger threshold (clamp 500-15000)."""
        self.trigger_threshold = max(500, min(15000, value))
        self.silence_threshold = self.trigger_threshold // 2

    def set_mic_gain(self, db):
        """Adjust ES8311 PGA gain."""
        if self._codec:
            self._codec.set_mic_gain(db)

    def pause(self):
        """Pause audio capture (for power saving)."""
        if self._state == STATE_RECORDING:
            self._stop_recording()
        self._paused = True

    def resume(self):
        """Resume audio capture."""
        self._paused = False
        self._trigger_count = 0
        self._pre_buf.clear()


# ─── WAV file utilities ──────────────────────────────────────────

def _write_wav_header(f, sample_rate, num_samples):
    """Write 44-byte WAV header. num_samples=0 for placeholder."""
    data_size = num_samples * 2
    file_size = 36 + data_size
    f.write(b'RIFF')
    f.write(struct.pack('<I', file_size))
    f.write(b'WAVE')
    f.write(b'fmt ')
    f.write(struct.pack('<I', 16))
    f.write(struct.pack('<H', 1))                    # PCM
    f.write(struct.pack('<H', 1))                    # mono
    f.write(struct.pack('<I', sample_rate))
    f.write(struct.pack('<I', sample_rate * 2))      # byte rate
    f.write(struct.pack('<H', 2))                    # block align
    f.write(struct.pack('<H', 16))                   # bits per sample
    f.write(b'data')
    f.write(struct.pack('<I', data_size))


def _finalize_wav_header(filepath, sample_rate, num_samples):
    """Seek back and update RIFF size + data size in WAV header."""
    data_size = num_samples * 2
    file_size = 36 + data_size
    with open(filepath, "r+b") as f:
        f.seek(4)
        f.write(struct.pack('<I', file_size))
        f.seek(40)
        f.write(struct.pack('<I', data_size))
