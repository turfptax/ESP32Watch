"""
ES8311 Low-Power Mono Audio Codec Driver for MicroPython
I2C driver for ADC (microphone recording) on the
Waveshare ESP32-S3-Touch-AMOLED-2.06.

The ES8311 is always powered on this board (no gate on CODEC_EN).
GPIO46 is actually the speaker amplifier (PA) enable — keep LOW for mic-only.
MCLK signal on GPIO41 is generated via PWM at 256 * sample_rate.

Register values derived from Espressif's esp-bsp es8311.c driver
for MCLK=4.096 MHz, Fs=16 kHz, slave mode, I2S 16-bit format.

Usage:
    from machine import I2C, Pin
    import board_config as BOARD
    from drivers.es8311 import ES8311

    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)
    codec = ES8311(i2c)
    codec.init()                # Powers up codec, starts MCLK, configures ADC
    codec.set_mic_gain(24)      # 24 dB PGA gain
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
_REG_SYS10   = 0x10   # System
_REG_SYS11   = 0x11   # System
_REG_SYS12   = 0x12   # System
_REG_SYS13   = 0x13   # System
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
    """ES8311 codec driver — ADC (microphone) focused."""

    def __init__(self, i2c, addr=None):
        self._i2c = i2c
        self._addr = addr or BOARD.AUDIO_ADDR
        self._mclk_pwm = None
        self._codec_en = None
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
        """Full power-up: enable codec, start MCLK, configure for 16 kHz ADC."""
        # 1. GPIO46 is the speaker amplifier (PA) enable, NOT codec power.
        # Keep it LOW to silence the speaker — we only need the mic/ADC.
        self._codec_en = Pin(BOARD.CODEC_EN, Pin.OUT, value=0)
        time.sleep_ms(10)

        # 2. Skip probe — ES8311 shares addr 0x18 with FT3168 touch.
        # After a failed import (stale touch state), probe reads get
        # responses from the touch controller instead of the codec.
        # The codec is confirmed always present at 0x18 on this board.
        print(f"ES8311: using addr 0x{self._addr:02X}")

        # 3. Start MCLK via PWM: 256 * 16000 = 4.096 MHz
        self._mclk_pwm = PWM(Pin(BOARD.I2S_MCLK),
                             freq=BOARD.AUDIO_MCLK_FREQ,
                             duty_u16=32768)  # 50% duty
        time.sleep_ms(10)

        # 4. Soft reset
        self._write_reg(_REG_RESET, 0x1F)
        time.sleep_ms(20)
        self._write_reg(_REG_RESET, 0x00)
        time.sleep_ms(20)

        # 5. Set GPIO44 for internal reference (noise immunity)
        self._write_reg(_REG_GPIO44, 0x08)

        # 6. Configure clock manager
        self._config_clocks()

        # 7. Configure I2S format: slave, I2S standard, 16-bit
        self._config_format()

        # 8. Power up analog circuitry and ADC
        self._power_up_adc()

        # 9. Set default mic gain
        self.set_mic_gain(BOARD.AUDIO_MIC_GAIN_DB)

        # 10. Set ADC digital volume to 0 dB
        self.set_adc_volume(0xBF)

        # 11. Mute DAC output — we only use the ADC (mic) side
        self._write_reg(_REG_DAC31, 0x00)     # DAC digital volume = 0
        self._write_reg(_REG_SDP_IN, 0x4C)    # Mute DAC input (bit6=1)

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

    def _power_up_adc(self):
        """Power up analog reference, PGA, and ADC modulator."""
        # REG0D: power up analog, enable VMID
        self._write_reg(_REG_SYS0D, 0x01)
        time.sleep_ms(50)  # Wait for VMID to settle

        # REG0E: enable ADC modulator (bit1=1), PGA power on (bit6=0)
        self._write_reg(_REG_SYS0E, 0x02)

        # REG12: ADC soft ramp
        self._write_reg(_REG_SYS12, 0x00)

        # REG14: select analog MIC (bit6=0 for analog, bit5:4 for LINSEL)
        self._write_reg(_REG_SYS14, 0x1A)

        # REG15: ADC HPF enable, ramp rate
        self._write_reg(_REG_ADC15, 0x40)

        # REG1C: ADC equalizer bypass
        self._write_reg(_REG_ALC1C, 0x6A)

        # REG44: set internal reference
        self._write_reg(_REG_GPIO44, 0x08)

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
        # Restart MCLK
        self._mclk_pwm = PWM(Pin(BOARD.I2S_MCLK),
                             freq=BOARD.AUDIO_MCLK_FREQ,
                             duty_u16=32768)
        time.sleep_ms(10)
        # Re-run clock + power-up config
        self._config_clocks()
        self._power_up_adc()
        self.mute(False)
        self.set_mic_gain(BOARD.AUDIO_MIC_GAIN_DB)
        self.set_adc_volume(0xBF)
        self._powered = True

    def deinit(self):
        """Full power down and disable codec."""
        self.standby()
        # Disable codec power
        if self._codec_en:
            self._codec_en(0)
        self._write_reg(_REG_GP45, 0x01)

    @property
    def is_powered(self):
        return self._powered
