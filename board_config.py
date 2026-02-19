"""
Board configuration for Waveshare ESP32-S3-Touch-AMOLED-2.06
All GPIO pin mappings and hardware constants.
"""

# ─── Display (CO5300 AMOLED via QSPI) ───────────────────────────
LCD_SDIO0  = 4    # QSPI Data 0
LCD_SDIO1  = 5    # QSPI Data 1
LCD_SDIO2  = 6    # QSPI Data 2
LCD_SDIO3  = 7    # QSPI Data 3
LCD_SCLK   = 11   # QSPI Clock
LCD_CS     = 12   # Chip Select
LCD_RESET  = 8    # Reset (active low)

LCD_WIDTH      = 410
LCD_HEIGHT     = 502
LCD_COL_OFFSET = 20   # Confirmed: display starts at column 20 in controller RAM
LCD_ROW_OFFSET = 0

# ─── I2C Bus (shared by touch, IMU, RTC, audio, PMIC) ───────────
I2C_SDA    = 15
I2C_SCL    = 14
I2C_FREQ   = 400_000  # 400 kHz

# ─── I2C Device Addresses ───────────────────────────────────────
TOUCH_ADDR   = 0x18   # FT3168 capacitive touch (some boards use 0x38)
IMU_ADDR     = 0x6B   # QMI8658 6-axis IMU
RTC_ADDR     = 0x51   # PCF85063A real-time clock
AUDIO_ADDR   = 0x18   # ES8311 audio codec (CE low=0x18, CE high=0x19, needs CODEC_EN)
PMIC_ADDR    = 0x34   # AXP2101 power management
EXPANDER_ADDR = 0x40  # TCA9554 I2C GPIO expander

# ─── Touch Controller (FT3168) ──────────────────────────────────
TP_INT     = 38   # Touch interrupt (active low)
TP_RESET   = 9    # Touch reset (active low)

# ─── Audio (ES8311 + I2S) ───────────────────────────────────────
I2S_MCLK   = 41
I2S_BCLK   = 45
I2S_DOUT   = 40   # Speaker output
I2S_DIN    = 42   # Microphone input
I2S_WS     = 16   # Word select
CODEC_EN   = 46   # Audio codec enable (set HIGH to enable)

# ─── Audio Recording Defaults ─────────────────────────────────────
AUDIO_SAMPLE_RATE    = 16_000      # 16 kHz — good for voice/barks
AUDIO_MCLK_FREQ     = 4_096_000   # 256 * 16000 Hz
AUDIO_MIC_GAIN_DB    = 24          # PGA gain for MEMS mic
AUDIO_TRIGGER_THRESH = 3000        # RMS level to start recording
AUDIO_SILENCE_THRESH = 1500        # RMS level to detect silence
AUDIO_PRE_BUFFER_MS  = 1500        # Circular pre-buffer length
AUDIO_SILENCE_MS     = 1500        # Silence duration to stop recording
AUDIO_MAX_CLIP_SEC   = 30          # Safety cap per clip
CLIPS_DIR            = "/sd/clips" # Where WAV files are saved

# ─── SD Card (SPI slot=3) ────────────────────────────────────────
SD_CLK     = 2
SD_CMD     = 1
SD_DATA    = 3
SD_CS      = 17
SD_SLOT    = 2    # slot=2 works on this board (slot=3 gives ESP_ERR_INVALID_STATE)

# ─── Buttons ────────────────────────────────────────────────────
BOOT_BTN   = 0    # GPIO0 (low = pressed)
# PWR button is on EXIO6 via TCA9554 I2C GPIO expander

# ─── Display Color Constants ────────────────────────────────────
COLOR_BLACK   = 0x0000
COLOR_WHITE   = 0xFFFF
COLOR_RED     = 0xF800
COLOR_GREEN   = 0x07E0
COLOR_BLUE    = 0x001F
COLOR_CYAN    = 0x07FF
COLOR_MAGENTA = 0xF81F
COLOR_YELLOW  = 0xFFE0
COLOR_ORANGE  = 0xFD20
COLOR_GRAY    = 0x8410

def rgb565(r, g, b):
    """Convert 8-bit RGB to 16-bit RGB565 (big-endian for display)."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

def rgb565_bytes(r, g, b):
    """Convert 8-bit RGB to 2-byte RGB565 (big-endian)."""
    c = rgb565(r, g, b)
    return bytes([c >> 8, c & 0xFF])
