"""
ES8311 Low-Power Mono Audio Codec Driver for MicroPython
I2C driver for DAC (speaker output) on the
Waveshare ESP32-S3-Touch-AMOLED-2.06.

Audio architecture on this board:
  - ES7210 (addr 0x40) → 4-ch ADC for dual MEMS microphones
  - ES8311 (this driver, addr 0x18) → DAC for speaker output

The ES8311 shares I2C address 0x18 with FT3168 touch.
GPIO46 (PA_EN) is the speaker amplifier enable — set HIGH to play audio.
MCLK is on GPIO16 (shared with ES7210, started by ES7210 driver).

NOTE: For mic recording, use drivers/es7210.py instead.
This driver is kept for future speaker/playback functionality.

Register values derived from Espressif's esp-bsp es8311.c driver
for MCLK=4.096 MHz, Fs=16 kHz, slave mode, I2S 16-bit format.

Usage:
    from machine import I2C, Pin
    import board_config as BOARD
    from drivers.es8311 import ES8311

    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)
    codec = ES8311(i2c)
    codec.init()                # Powers up codec for DAC output
"""

import time
from machine import Pin, PWM

import board_config as BOARD


# ─── ES8311 Register Map ─────────────────────────────────────────
_REG_RESET   = 0x00   # Reset / clock source / master-slave
_REG_CLK01   = 0x01   # Clock manager: gate, MCLK source
_REG_CLK02   = 0x02   # Clock manager: pre_div, pre_multi
_REG_CLK03   = 0x03   # Clock manager: fs_mode, ADC OSR
_REG_CLK04   = 0x04   # Clock manager: DAC OSR
_REG_CLK05   = 0x05   # Clock manager: ADC_div, DAC_div
_REG_CLK06   = 0x06   # Clock manager: BCLK divider
_REG_CLK07   = 0x07   # Clock manager: LRCK divider high
_REG_CLK08   = 0x08   # Clock manager: LRCK divider low

_REG_SDP_IN  = 0x09   # Serial data port input (DAC)
_REG_SDP_OUT = 0x0A   # Serial data port output (ADC)

_REG_SYS0B   = 0x0B   # System
_REG_SYS0C   = 0x0C   # System
_REG_SYS0D   = 0x0D   # System: power, VMID
_REG_SYS0E   = 0x0E   # System: ADC enable, PGA power
_REG_SYS0F   = 0x0F   # System
_REG_SYS10   = 0x10   # System: power config
_REG_SYS11   = 0x11   # System: power config
_REG_SYS12   = 0x12   # System: DAC power
_REG_SYS13   = 0x13   # System: HP drive enable
_REG_SYS14   = 0x14   # System: analog input select, MIC type

_REG_ADC15   = 0x15   # ADC: ramp rate, HPF
_REG_ADC16   = 0x16   # ADC: MIC PGA gain
_REG_ADC17   = 0x17   # ADC: digital volume
_REG_ALC18   = 0x18   # ALC: enable, target level
_REG_ALC19   = 0x19   # ALC: max/min gain
_REG_ALC1A   = 0x1A   # ALC: noise gate
_REG_ALC1B   = 0x1B   # ALC: more control
_REG_ALC1C   = 0x1C   # ADC: HPF / equalizer

_REG_DAC31   = 0x31   # DAC: volume
_REG_DAC32   = 0x32   # DAC: DRC / automute
_REG_DAC37   = 0x37   # DAC: EQ

_REG_GPIO44  = 0x44   # GPIO / internal reference
_REG_GP45    = 0x45   # GP control

_REG_CHD1    = 0xFD   # Chip ID 1
_REG_CHD2    = 0xFE   # Chip ID 2
_REG_CHVER   = 0xFF   # Chip version

# ─── Coefficient table entry for MCLK=4096000, Fs=16000 ─────────
# From Espressif esp-bsp coeff_div table:
# {4096000, 16000, pre_div=1, pre_multi=0, adc_div=1, dac_div=1,
#  fs_mode=0, lrck_h=0x00, lrck_l=0xFF, bclk_div=4, adc_osr=0x10, dac_osr=0x10}
_COEFF_PRE_DIV   = 1
_COEFF_PRE_MULTI = 0
_COEFF_ADC_DIV   = 1
_COEFF_DAC_DIV   = 1
_COEFF_FS_MODE   = 0
_COEFF_LRCK_H    = 0x00
_COEFF_LRCK_L    = 0xFF
_COEFF_BCLK_DIV  = 4
_COEFF_ADC_OSR   = 0x10
_COEFF_DAC_OSR   = 0x10

# PGA gain values (register 0x16 direct write)
# Each step is approx 3 dB.  Values from Espressif enum:
_MIC_GAIN_TABLE = {
    0: 0x00, 6: 0x01, 12: 0x02, 18: 0x03,
    24: 0x04, 30: 0x05, 36: 0x06, 42: 0x07,
}


class ES8311:
    """ES8311 codec driver — DAC (speaker output).

    NOTE: Microphone input uses ES7210 (drivers/es7210.py), not this codec.
    """

    def __init__(self, i2c, addr=None):
        self._i2c = i2c
        self._addr = addr or BOARD.ES8311_ADDR
        self._mclk_pwm = None
        self._pa_pin = None
        self._powered = False

    # ─── Low-level I2C ────────────────────────────────────────────

    def _read_reg(self, reg):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, 1)[0]

    def _write_reg(self, reg, val):
        self._i2c.writeto(self._addr, bytes([reg, val & 0xFF]))

    def _update_reg(self, reg, mask, val):
        """Read-modify-write: clear bits in mask, set bits in val."""
        cur = self._read_reg(reg)
        self._write_reg(reg, (cur & ~mask) | (val & mask))

    def _probe(self):
        """Check if ES8311 responds at current address by reading chip ID."""
        try:
            # Read multiple ID registers to be sure
            id1 = self._read_reg(_REG_CHD1)   # 0xFD — expect 0x83
            id2 = self._read_reg(_REG_CHD2)   # 0xFE
            ver = self._read_reg(_REG_CHVER)   # 0xFF
            print(f"ES8311 probe 0x{self._addr:02X}: "
                  f"ID1=0x{id1:02X} ID2=0x{id2:02X} VER=0x{ver:02X}")
            # ES8311 chip ID1 is typically 0x83
            return id1 != 0x00 and id1 != 0xFF
        except OSError as e:
            print(f"ES8311 probe 0x{self._addr:02X}: {e}")
            return False

    # ─── Initialization ───────────────────────────────────────────

    def init(self):
        """Full power-up: configure ES8311 for DAC output.

        Register sequence follows Espressif esp-adf es8311.c reference driver.
        MCLK is assumed to already be running (started by ES7210 driver).
        PA (GPIO46) starts LOW — call enable_speaker() to enable.
        """
        # 1. Speaker PA starts off
        self._pa_pin = Pin(BOARD.PA_EN, Pin.OUT, value=0)
        time.sleep_ms(10)

        # 2. Skip probe — ES8311 shares addr 0x18 with FT3168 touch.
        print(f"ES8311: using addr 0x{self._addr:02X} (DAC mode)")

        # ── es8311_codec_init() sequence ──

        # REG44: initial GPIO/reference config
        self._write_reg(_REG_GPIO44, 0x08)
        # REG01: clock gates on, MCLK from pin
        self._write_reg(_REG_CLK01, 0x30)
        # REG02-05: clock dividers (reset defaults OK for init)
        self._write_reg(_REG_CLK02, 0x00)
        self._write_reg(_REG_CLK03, 0x10)
        self._write_reg(_REG_CLK04, 0x10)
        self._write_reg(_REG_CLK05, 0x00)
        # REG0B/0C: system registers (required by ref driver)
        self._write_reg(_REG_SYS0B, 0x00)
        self._write_reg(_REG_SYS0C, 0x00)
        # REG10/11: system power config
        self._write_reg(_REG_SYS10, 0x1F)
        self._write_reg(_REG_SYS11, 0x7F)
        # REG00: power on (0x80), then configure slave mode (clear bit6)
        self._write_reg(_REG_RESET, 0x80)
        time.sleep_ms(20)

        # ── Configure clocks for our specific MCLK/Fs ──
        self._config_clocks()

        # ── Configure I2S format: slave, I2S standard, 16-bit ──
        self._config_format()

        # ── es8311_codec_init() continued ──
        # REG13: enable output to HP drive
        self._write_reg(_REG_SYS13, 0x10)
        # REG1B: ALC config
        self._write_reg(_REG_ALC1B, 0x0A)
        # REG1C: ADC EQ bypass, cancel DC offset
        self._write_reg(_REG_ALC1C, 0x6A)

        # ── es8311_start() for ADC/MIC mode ──
        # Unmute ADC output (SDP_OUT bit6=0)
        self._write_reg(_REG_SDP_OUT, 0x0C)
        # ADC digital volume
        self._write_reg(_REG_ADC17, 0xBF)
        # REG0E: enable ADC modulator
        self._write_reg(_REG_SYS0E, 0x02)
        # REG12: power up DAC path (needed even for ADC-only)
        self._write_reg(_REG_SYS12, 0x00)
        # REG14: select analog MIC input
        self._write_reg(_REG_SYS14, 0x1A)
        # REG0D: power up analog, enable VMID
        self._write_reg(_REG_SYS0D, 0x01)
        time.sleep_ms(50)  # Wait for VMID to settle
        # REG15: ADC HPF enable
        self._write_reg(_REG_ADC15, 0x40)
        # REG37: DAC EQ bypass
        self._write_reg(_REG_DAC37, 0x08)
        # REG45: GP control
        self._write_reg(_REG_GP45, 0x00)
        # REG44: switch to ADC active reference (critical for mic data!)
        self._write_reg(_REG_GPIO44, 0x58)

        # Set mic PGA gain
        self.set_mic_gain(BOARD.AUDIO_MIC_GAIN_DB)

        # Mute DAC output — we only use the ADC (mic) side
        self._write_reg(_REG_DAC31, 0x00)
        self._write_reg(_REG_SDP_IN, 0x4C)  # Mute DAC input (bit6=1)

        self._powered = True
        print("ES8311: initialized (16 kHz ADC, slave mode)")

    def _config_clocks(self):
        """Configure clocks for MCLK=4.096 MHz, Fs=16 kHz."""
        # REG01: enable all clock gates, MCLK from MCLK pin (bit7=0)
        self._write_reg(_REG_CLK01, 0x3F)

        # REG02: pre_div and pre_multi
        # Bits [7:5] = pre_div - 1, bits [4:3] = pre_multi, bits [2:0] preserved
        reg02 = self._read_reg(_REG_CLK02)
        reg02 &= 0x07
        reg02 |= ((_COEFF_PRE_DIV - 1) << 5)
        reg02 |= (_COEFF_PRE_MULTI << 3)
        self._write_reg(_REG_CLK02, reg02)

        # REG03: fs_mode [7:6], ADC OSR [5:0]
        self._write_reg(_REG_CLK03,
                        (_COEFF_FS_MODE << 6) | _COEFF_ADC_OSR)

        # REG04: DAC OSR
        self._write_reg(_REG_CLK04, _COEFF_DAC_OSR)

        # REG05: ADC_div [7:4], DAC_div [3:0]
        self._write_reg(_REG_CLK05,
                        ((_COEFF_ADC_DIV - 1) << 4) | (_COEFF_DAC_DIV - 1))

        # REG06: BCLK divider
        reg06 = self._read_reg(_REG_CLK06)
        reg06 &= 0xE0
        reg06 |= (_COEFF_BCLK_DIV & 0x1F)
        self._write_reg(_REG_CLK06, reg06)

        # REG07/08: LRCK divider
        self._write_reg(_REG_CLK07, _COEFF_LRCK_H)
        self._write_reg(_REG_CLK08, _COEFF_LRCK_L)

    def _config_format(self):
        """Configure I2S slave mode, standard I2S, 16-bit."""
        # REG00 bit6: 0=slave, 1=master.  Keep slave.
        reg00 = self._read_reg(_REG_RESET)
        reg00 &= 0xBF  # Clear bit 6 → slave mode
        self._write_reg(_REG_RESET, reg00)

        # REG0A (SDP out / ADC side): I2S standard, 16-bit
        # Bits [6]: 0 = not muted
        # Bits [4:3]: 00 = I2S format
        # Bits [2:1]: 11 = 16-bit word length
        self._write_reg(_REG_SDP_OUT, 0x0C)

        # REG09 (SDP in / DAC side): same format (for completeness)
        self._write_reg(_REG_SDP_IN, 0x0C)

    # ─── Public API ───────────────────────────────────────────────

    def set_mic_gain(self, db):
        """Set MIC PGA gain in dB (0, 6, 12, 18, 24, 30, 36, 42)."""
        # Clamp to nearest valid value
        valid = sorted(_MIC_GAIN_TABLE.keys())
        best = valid[0]
        for v in valid:
            if v <= db:
                best = v
        self._write_reg(_REG_ADC16, _MIC_GAIN_TABLE[best])

    def set_adc_volume(self, vol):
        """Set ADC digital volume (0x00=min, 0xBF=0dB, 0xFF=max)."""
        self._write_reg(_REG_ADC17, vol)

    def mute(self, enable=True):
        """Mute or unmute the ADC output."""
        if enable:
            self._update_reg(_REG_SDP_OUT, 0x40, 0x40)  # Set bit 6
        else:
            self._update_reg(_REG_SDP_OUT, 0x40, 0x00)  # Clear bit 6

    def standby(self):
        """Power down ADC and stop MCLK to save power."""
        if not self._powered:
            return
        # Mute ADC output
        self.mute(True)
        # Power down ADC
        self._write_reg(_REG_ADC17, 0x00)
        self._write_reg(_REG_SYS0E, 0xFF)
        self._write_reg(_REG_SYS12, 0x02)
        self._write_reg(_REG_SYS14, 0x00)
        self._write_reg(_REG_SYS0D, 0xFA)
        self._write_reg(_REG_ADC15, 0x00)
        # Stop MCLK
        if self._mclk_pwm:
            self._mclk_pwm.deinit()
            self._mclk_pwm = None
        self._powered = False

    def resume(self):
        """Re-init after standby."""
        if self._powered:
            return
        self.init()

    def deinit(self):
        """Full power down and disable speaker PA."""
        self.standby()
        # Disable speaker PA
        if self._pa_pin:
            self._pa_pin(0)
        self._write_reg(_REG_GP45, 0x01)

    @property
    def is_powered(self):
        return self._powered
