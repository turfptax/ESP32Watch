"""
Audio Hardware Validation Script
Run from REPL to verify ES7210 ADC and I2S microphone input.

This board uses a dual-codec architecture:
  - ES7210 (addr 0x40) → 4-ch ADC for dual MEMS microphones
  - ES8311 (addr 0x18) → DAC for speaker output

Usage (paste into REPL or run with: import test_audio):
    import test_audio
    test_audio.test_codec_init()    # Verify ES7210 on I2C
    test_audio.test_live_audio()    # Print live amplitude values
    test_audio.test_record_wav()    # Record 5s WAV to SD card
"""

import time
import struct
from machine import Pin, I2C, I2S, PWM

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


def test_i2c_scan():
    """Debug helper: full I2C scan showing all devices."""
    print("=" * 40)
    print("DEBUG: Full I2C Scan")
    print("=" * 40)

    i2c = _init_i2c()
    addrs = i2c.scan()
    print("I2C addresses found:", [hex(a) for a in addrs])

    known = {
        0x18: "FT3168 touch + ES8311 DAC (shared)",
        0x34: "AXP2101 PMIC",
        0x40: "TCA9554 expander + ES7210 ADC (shared)",
        0x51: "PCF85063A RTC",
        0x6B: "QMI8658 IMU",
    }
    for addr in addrs:
        name = known.get(addr, "Unknown")
        print(f"  0x{addr:02X}: {name}")

    # Check for ES7210 at 0x40
    if 0x40 in addrs:
        print("\nES7210 should be at 0x40 (shared with TCA9554)")
    else:
        print("\nWARNING: 0x40 not found — ES7210 may not be accessible!")


def test_codec_init():
    """Test 1: Initialize ES7210 ADC and verify MCLK + I2C.

    Returns (codec, i2c) on success, False on failure.
    """
    print("=" * 40)
    print("TEST: ES7210 ADC Init")
    print("=" * 40)

    i2c = _init_i2c()

    # Silence speaker PA
    pa = Pin(BOARD.PA_EN, Pin.OUT, value=0)
    print(f"Speaker PA (GPIO{BOARD.PA_EN}): OFF")

    # Scan I2C
    addrs = i2c.scan()
    print("I2C addresses:", [hex(a) for a in addrs])

    if BOARD.ES7210_ADDR not in addrs:
        print(f"ERROR: ES7210 addr 0x{BOARD.ES7210_ADDR:02X} not found!")
        return False

    # Init ES7210
    from drivers.es7210 import ES7210
    codec = ES7210(i2c)
    codec.init()
    print(f"PASS: ES7210 initialized at 0x{BOARD.ES7210_ADDR:02X}")

    return codec, i2c


def test_live_audio(duration_sec=10):
    """Test 2: Read I2S audio and print RMS amplitude.

    Clap or make noise to see amplitude spikes.
    Press Ctrl+C to stop early.

    I2S returns stereo data (L=MIC1, R=MIC2). We display both channels.
    """
    print("=" * 40)
    print("TEST: Live Audio (I2S from ES7210)")
    print("=" * 40)

    result = test_codec_init()
    if result is False:
        return
    codec, i2c = result

    # Start I2S in receive mode (stereo — ES7210 outputs L+R)
    i2s = I2S(0,
              sck=Pin(BOARD.I2S_SCLK),
              ws=Pin(BOARD.I2S_LRCLK),
              sd=Pin(BOARD.I2S_DIN),
              mode=I2S.RX,
              bits=16,
              format=I2S.STEREO,
              rate=BOARD.AUDIO_SAMPLE_RATE,
              ibuf=8192)

    # Let DMA fill
    time.sleep_ms(200)

    # Quick test: read raw data
    test_buf = bytearray(256)
    test_n = i2s.readinto(test_buf)
    print(f"I2S test read: {test_n} bytes")
    if test_n > 0:
        print(f"  First 16 bytes hex: {test_buf[:16].hex()}")
        # Check if data is non-zero
        nonzero = any(b != 0 for b in test_buf[:test_n])
        print(f"  Contains non-zero data: {nonzero}")

    buf = bytearray(1024)  # Stereo: 256 frames = 16ms at 16 kHz
    print(f"\nReading audio for {duration_sec}s... (clap to test)")
    print("  RMS_L | RMS_R | Bar")
    print("-" * 50)

    try:
        end = time.ticks_add(time.ticks_ms(), duration_sec * 1000)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            n = i2s.readinto(buf)
            if n == 0:
                continue

            # Calculate RMS for left and right channels separately
            n_frames = n // 4  # 4 bytes per stereo frame
            sum_sq_l = 0
            sum_sq_r = 0
            for i in range(0, n - 3, 4):
                sample_l = struct.unpack_from('<h', buf, i)[0]
                sample_r = struct.unpack_from('<h', buf, i + 2)[0]
                sum_sq_l += sample_l * sample_l
                sum_sq_r += sample_r * sample_r

            rms_l = _isqrt(sum_sq_l // max(n_frames, 1))
            rms_r = _isqrt(sum_sq_r // max(n_frames, 1))

            # Visual bar (left channel, scaled to ~30 chars)
            bar_len = min(rms_l // 300, 30)
            bar = "#" * bar_len

            print(f"  {rms_l:5d} | {rms_r:5d} | {bar}")
            time.sleep_ms(100)

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        i2s.deinit()
        codec.deinit()
        print("Audio hardware released")


def test_record_wav(seconds=5, filename="/sd/test_recording.wav"):
    """Test 3: Record a mono WAV file to SD card.

    Records left channel (MIC1) from ES7210 stereo I2S output.
    Requires SD card mounted at /sd.
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

    # Start I2S (stereo)
    i2s = I2S(0,
              sck=Pin(BOARD.I2S_SCLK),
              ws=Pin(BOARD.I2S_LRCLK),
              sd=Pin(BOARD.I2S_DIN),
              mode=I2S.RX,
              bits=16,
              format=I2S.STEREO,
              rate=BOARD.AUDIO_SAMPLE_RATE,
              ibuf=8192)

    # Let DMA fill
    time.sleep_ms(200)

    sample_rate = BOARD.AUDIO_SAMPLE_RATE
    total_samples = sample_rate * seconds

    try:
        with open(filename, "wb") as f:
            # Write WAV header (mono, placeholder data size)
            _write_wav_header(f, sample_rate, 0)

            stereo_buf = bytearray(1024)
            samples_written = 0
            last_print = time.ticks_ms()

            print("Recording... ", end="")
            while samples_written < total_samples:
                n = i2s.readinto(stereo_buf)
                if n <= 0:
                    continue

                # Extract left channel (MIC1) from stereo
                for i in range(0, n - 3, 4):
                    if samples_written >= total_samples:
                        break
                    f.write(bytes([stereo_buf[i], stereo_buf[i + 1]]))
                    samples_written += 1

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


def test_raw_i2s():
    """Test 4: Raw I2S read without codec init — verify pin wiring.

    Starts MCLK via PWM, configures I2S, reads raw bytes.
    Useful for debugging when codec init might be the problem.
    """
    print("=" * 40)
    print("TEST: Raw I2S Pin Test")
    print("=" * 40)

    i2c = _init_i2c()

    # Silence speaker
    pa = Pin(BOARD.PA_EN, Pin.OUT, value=0)

    # Start MCLK on GPIO16
    print(f"Starting MCLK on GPIO{BOARD.I2S_MCLK} at {BOARD.AUDIO_MCLK_FREQ}Hz...")
    mclk = PWM(Pin(BOARD.I2S_MCLK), freq=BOARD.AUDIO_MCLK_FREQ, duty_u16=32768)
    time.sleep_ms(50)

    # Check DIN pin state before I2S init
    din = Pin(BOARD.I2S_DIN, Pin.IN)
    print(f"DIN (GPIO{BOARD.I2S_DIN}) state before I2S: {din()}")

    # Init I2S
    print(f"I2S config: SCLK=GPIO{BOARD.I2S_SCLK}, LRCLK=GPIO{BOARD.I2S_LRCLK}, DIN=GPIO{BOARD.I2S_DIN}")
    i2s = I2S(0,
              sck=Pin(BOARD.I2S_SCLK),
              ws=Pin(BOARD.I2S_LRCLK),
              sd=Pin(BOARD.I2S_DIN),
              mode=I2S.RX,
              bits=16,
              format=I2S.STEREO,
              rate=BOARD.AUDIO_SAMPLE_RATE,
              ibuf=8192)

    time.sleep_ms(200)

    # Read raw data
    buf = bytearray(128)
    n = i2s.readinto(buf)
    print(f"\nRead {n} bytes")
    print(f"Raw hex: {buf[:min(n, 64)].hex()}")

    # Check for non-zero
    nonzero = any(b != 0 for b in buf[:n])
    print(f"Non-zero data: {nonzero}")

    if n > 0:
        # Parse first few stereo samples
        print("\nFirst 8 stereo frames (L, R):")
        for i in range(0, min(n, 32), 4):
            l = struct.unpack_from('<h', buf, i)[0]
            r = struct.unpack_from('<h', buf, i + 2)[0]
            print(f"  L={l:6d}  R={r:6d}")

    i2s.deinit()
    mclk.deinit()
    print("\nHardware released")


# ─── Helpers ──────────────────────────────────────────────────────

def _isqrt(n):
    """Integer square root via Newton's method."""
    if n <= 0:
        return 0
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return x


# ─── WAV helpers ──────────────────────────────────────────────────

def _write_wav_header(f, sample_rate, num_samples):
    """Write 44-byte WAV header (mono, 16-bit PCM)."""
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
    print("  test_audio.test_i2c_scan()     - full I2C scan")
    print("  test_audio.test_codec_init()   - verify ES7210 ADC")
    print("  test_audio.test_live_audio()   - live amplitude monitor")
    print("  test_audio.test_record_wav()   - record 5s WAV to SD")
    print("  test_audio.test_raw_i2s()      - raw I2S pin test")
