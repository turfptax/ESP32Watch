"""
Audio Hardware Validation Script
Run from REPL to verify ES8311 codec and I2S microphone input.

Usage (paste into REPL or run with: import test_audio):
    import test_audio
    test_audio.test_codec_init()    # Verify ES8311 on I2C
    test_audio.test_live_audio()    # Print live amplitude values
    test_audio.test_record_wav()    # Record 5s WAV to SD card
"""

import time
import struct
from machine import Pin, I2C, I2S

import board_config as BOARD


def _init_i2c():
    """Create shared I2C bus and enable TCA9554 (peripheral power)."""
    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)

    # TCA9554: all outputs HIGH (required for peripherals)
    try:
        i2c.writeto(BOARD.EXPANDER_ADDR, bytes([0x03, 0x00]))  # Config: outputs
        i2c.writeto(BOARD.EXPANDER_ADDR, bytes([0x01, 0xFF]))  # All HIGH
        print("TCA9554: outputs enabled")
    except Exception as e:
        print(f"TCA9554 warning: {e}")

    return i2c


def test_codec_init():
    """Test 1: Verify ES8311 appears on I2C and initializes."""
    print("=" * 40)
    print("TEST: ES8311 Codec Init")
    print("=" * 40)

    i2c = _init_i2c()

    # Scan before enabling codec
    print("I2C scan (codec disabled):", [hex(a) for a in i2c.scan()])

    # Enable codec power
    codec_en = Pin(BOARD.CODEC_EN, Pin.OUT, value=1)
    time.sleep_ms(50)

    # Scan after enabling codec
    addrs = i2c.scan()
    print("I2C scan (codec enabled):", [hex(a) for a in addrs])

    if BOARD.AUDIO_ADDR in addrs:
        print(f"  ES8311 found at 0x{BOARD.AUDIO_ADDR:02X}")
    else:
        print("  ERROR: ES8311 not found!")
        codec_en(0)
        return False

    # Init codec
    from drivers.es8311 import ES8311
    codec = ES8311(i2c)
    codec.init()

    # Read back a register to verify
    chip_ver = codec._read_reg(0xFF)
    print(f"  Chip version: 0x{chip_ver:02X}")
    print("  PASS: ES8311 initialized")

    return codec, i2c


def test_live_audio(duration_sec=10):
    """Test 2: Read I2S audio and print RMS amplitude.

    Clap or make noise to see amplitude spikes.
    Press Ctrl+C to stop early.
    """
    print("=" * 40)
    print("TEST: Live Audio (I2S)")
    print("=" * 40)

    result = test_codec_init()
    if result is False:
        return
    codec, i2c = result

    # Start I2S in receive mode
    i2s = I2S(0,
              sck=Pin(BOARD.I2S_BCLK),
              ws=Pin(BOARD.I2S_WS),
              sd=Pin(BOARD.I2S_DIN),
              mode=I2S.RX,
              bits=16,
              format=I2S.MONO,
              rate=BOARD.AUDIO_SAMPLE_RATE,
              ibuf=8192)

    buf = bytearray(1024)  # ~32ms of audio at 16 kHz
    print(f"Reading audio for {duration_sec}s... (clap to test)")
    print("  RMS   | Peak  | Bar")
    print("-" * 50)

    try:
        end = time.ticks_add(time.ticks_ms(), duration_sec * 1000)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            n = i2s.readinto(buf)
            if n == 0:
                continue

            # Calculate RMS and peak
            n_samples = n // 2
            sum_sq = 0
            peak = 0
            for i in range(0, n, 2):
                sample = struct.unpack_from('<h', buf, i)[0]
                abs_s = abs(sample)
                if abs_s > peak:
                    peak = abs_s
                sum_sq += sample * sample

            mean_sq = sum_sq // n_samples
            # Integer sqrt (Newton's method)
            rms = 0
            if mean_sq > 0:
                x = mean_sq
                y = (x + 1) // 2
                while y < x:
                    x = y
                    y = (x + mean_sq // x) // 2
                rms = x

            # Visual bar (scaled to ~30 chars, max around 10000)
            bar_len = min(rms // 300, 30)
            bar = "#" * bar_len

            print(f"  {rms:5d} | {peak:5d} | {bar}")
            time.sleep_ms(100)

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        i2s.deinit()
        codec.deinit()
        print("Audio hardware released")


def test_record_wav(seconds=5, filename="/sd/test_recording.wav"):
    """Test 3: Record a WAV file to SD card.

    Requires SD card mounted at /sd (run logger.init() first or
    mount manually).
    """
    import os
    print("=" * 40)
    print(f"TEST: Record {seconds}s WAV → {filename}")
    print("=" * 40)

    # Check SD card
    try:
        os.stat("/sd")
    except OSError:
        print("SD card not mounted. Mounting...")
        from logger import log
        log.init()
        try:
            os.stat("/sd")
        except OSError:
            print("ERROR: No SD card available")
            return

    result = test_codec_init()
    if result is False:
        return
    codec, i2c = result

    # Start I2S
    i2s = I2S(0,
              sck=Pin(BOARD.I2S_BCLK),
              ws=Pin(BOARD.I2S_WS),
              sd=Pin(BOARD.I2S_DIN),
              mode=I2S.RX,
              bits=16,
              format=I2S.MONO,
              rate=BOARD.AUDIO_SAMPLE_RATE,
              ibuf=8192)

    sample_rate = BOARD.AUDIO_SAMPLE_RATE
    total_samples = sample_rate * seconds
    bytes_per_sample = 2

    try:
        with open(filename, "wb") as f:
            # Write WAV header (placeholder data size)
            _write_wav_header(f, sample_rate, 0)

            buf = bytearray(1024)
            samples_written = 0
            last_print = time.ticks_ms()

            print("Recording... ", end="")
            while samples_written < total_samples:
                n = i2s.readinto(buf)
                if n > 0:
                    # Don't write more than needed
                    remaining = (total_samples - samples_written) * bytes_per_sample
                    write_n = min(n, remaining)
                    f.write(buf[:write_n])
                    samples_written += write_n // bytes_per_sample

                # Progress
                now = time.ticks_ms()
                if time.ticks_diff(now, last_print) > 1000:
                    elapsed = samples_written / sample_rate
                    print(f"{elapsed:.0f}s ", end="")
                    last_print = now

            print("done!")

        # Update WAV header with actual size
        _finalize_wav_header(filename, sample_rate, samples_written)

        file_size = os.stat(filename)[6]
        print(f"  Saved: {filename}")
        print(f"  Size: {file_size} bytes ({samples_written} samples)")
        print(f"  Duration: {samples_written / sample_rate:.1f}s")
        print("  Copy to PC and play to verify!")

    except Exception as e:
        print(f"\nERROR: {e}")
    finally:
        i2s.deinit()
        codec.deinit()
        print("Audio hardware released")


# ─── WAV helpers ──────────────────────────────────────────────────

def _write_wav_header(f, sample_rate, num_samples):
    """Write 44-byte WAV header."""
    data_size = num_samples * 2
    file_size = 36 + data_size

    f.write(b'RIFF')
    f.write(struct.pack('<I', file_size))
    f.write(b'WAVE')
    f.write(b'fmt ')
    f.write(struct.pack('<I', 16))           # fmt chunk size
    f.write(struct.pack('<H', 1))            # PCM
    f.write(struct.pack('<H', 1))            # mono
    f.write(struct.pack('<I', sample_rate))   # sample rate
    f.write(struct.pack('<I', sample_rate * 2))  # byte rate
    f.write(struct.pack('<H', 2))            # block align
    f.write(struct.pack('<H', 16))           # bits per sample
    f.write(b'data')
    f.write(struct.pack('<I', data_size))


def _finalize_wav_header(filepath, sample_rate, num_samples):
    """Seek back and update data size fields in WAV header."""
    data_size = num_samples * 2
    file_size = 36 + data_size
    with open(filepath, "r+b") as f:
        f.seek(4)
        f.write(struct.pack('<I', file_size))
        f.seek(40)
        f.write(struct.pack('<I', data_size))


# ─── Auto-run if imported directly ───────────────────────────────
if __name__ == "__main__" or __name__ == "test_audio":
    print("Audio test module loaded.")
    print("  test_audio.test_codec_init()   - verify ES8311")
    print("  test_audio.test_live_audio()   - live amplitude monitor")
    print("  test_audio.test_record_wav()   - record 5s WAV to SD")
