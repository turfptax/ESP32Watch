"""
AXP2101 Power Management IC Driver for MicroPython
Manages battery charging, voltage rails, and power monitoring
for the Waveshare ESP32-S3-Touch-AMOLED-2.06

Usage:
    from drivers.axp2101 import AXP2101
    from machine import I2C, Pin
    from board_config import *

    i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)
    pmic = AXP2101(i2c)
    pmic.init()

    print(f"Battery: {pmic.battery_percent}%")
    print(f"Charging: {pmic.is_charging}")
    print(f"VBUS present: {pmic.is_vbus_present}")
"""

import time
import board_config as BOARD


# ─── AXP2101 Register Map (subset for watch use) ────────────────
# Status registers
_REG_STATUS1       = 0x00  # System status
_REG_STATUS2       = 0x01  # Charge status
_REG_IC_TYPE       = 0x03  # Chip ID

# IRQ registers
_REG_IRQ_EN0       = 0x40
_REG_IRQ_EN1       = 0x41
_REG_IRQ_EN2       = 0x42
_REG_IRQ_STATUS0   = 0x48
_REG_IRQ_STATUS1   = 0x49
_REG_IRQ_STATUS2   = 0x4A

# Battery gauge
_REG_BAT_PERCENT   = 0xA4  # Battery level 0-100%

# ADC control
_REG_ADC_ENABLE    = 0x30
_REG_VBAT_H        = 0x34  # Battery voltage high byte
_REG_VBAT_L        = 0x35  # Battery voltage low byte
_REG_TS_H          = 0x36  # Temperature sensor high
_REG_TS_L          = 0x37  # Temperature sensor low
_REG_VBUS_H        = 0x38  # VBUS voltage high
_REG_VBUS_L        = 0x39  # VBUS voltage low
_REG_VSYS_H        = 0x3A  # System voltage high
_REG_VSYS_L        = 0x3B  # System voltage low

# Power output control
_REG_DCDC_ONOFF    = 0x80  # DCDC enable/disable
_REG_LDO_ONOFF0    = 0x90  # LDO enable/disable
_REG_LDO_ONOFF1    = 0x91

# DCDC voltage setting
_REG_DCDC1_VOL     = 0x82  # DCDC1 voltage
_REG_DCDC2_VOL     = 0x83  # DCDC2 voltage
_REG_DCDC3_VOL     = 0x84  # DCDC3 voltage
_REG_DCDC4_VOL     = 0x85  # DCDC4 voltage
_REG_DCDC5_VOL     = 0x86  # DCDC5 voltage

# LDO voltage setting
_REG_ALDO1_VOL     = 0x92
_REG_ALDO2_VOL     = 0x93
_REG_ALDO3_VOL     = 0x94
_REG_ALDO4_VOL     = 0x95
_REG_BLDO1_VOL     = 0x96
_REG_BLDO2_VOL     = 0x97
_REG_DLDO1_VOL     = 0x99
_REG_DLDO2_VOL     = 0x9A

# Charge control
_REG_CHG_CTL       = 0x62  # Charge enable
_REG_CHG_CURR      = 0x61  # Charge current setting
_REG_CHG_V_TERM    = 0x64  # Charge termination voltage

# Power key timing
_REG_PWROFF_EN     = 0x10  # Power off enable
_REG_PWRON_STATUS  = 0x20  # Power on source


class AXP2101:
    """AXP2101 PMIC driver for battery and power management."""

    def __init__(self, i2c, addr=None):
        self._i2c = i2c
        self._addr = addr or BOARD.PMIC_ADDR

    def init(self):
        """Initialize the PMIC and enable ADC readings."""
        # Verify chip presence
        chip_id = self._read_reg(_REG_IC_TYPE)
        if chip_id != 0x4B:  # AXP2101 ID
            print(f"Warning: unexpected PMIC chip ID 0x{chip_id:02X} (expected 0x4B)")
        else:
            print("AXP2101 PMIC detected")

        # Enable battery voltage and temperature ADC
        self._write_reg(_REG_ADC_ENABLE, 0x03)

        # Clear any pending IRQs
        self._write_reg(_REG_IRQ_STATUS0, 0xFF)
        self._write_reg(_REG_IRQ_STATUS1, 0xFF)
        self._write_reg(_REG_IRQ_STATUS2, 0xFF)

    # ─── I2C helpers ─────────────────────────────────────────────

    def _read_reg(self, reg):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, 1)[0]

    def _read_regs(self, reg, count):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, count)

    def _write_reg(self, reg, value):
        self._i2c.writeto(self._addr, bytes([reg, value]))

    def _set_bits(self, reg, mask):
        val = self._read_reg(reg)
        self._write_reg(reg, val | mask)

    def _clear_bits(self, reg, mask):
        val = self._read_reg(reg)
        self._write_reg(reg, val & ~mask)

    # ─── Battery status ──────────────────────────────────────────

    @property
    def battery_percent(self):
        """Battery level as 0-100 percent."""
        return self._read_reg(_REG_BAT_PERCENT) & 0x7F

    @property
    def battery_voltage(self):
        """Battery voltage in millivolts (approximate)."""
        data = self._read_regs(_REG_VBAT_H, 2)
        raw = (data[0] << 4) | (data[1] & 0x0F)
        # AXP2101 ADC: ~1.1mV per LSB, typical Li-ion range 3000-4200mV
        return int(raw * 1.1)

    @property
    def is_charging(self):
        """True if battery is currently charging."""
        status = self._read_reg(_REG_STATUS2)
        return bool(status & 0x60)  # Charge status bits

    @property
    def is_vbus_present(self):
        """True if USB power is connected."""
        status = self._read_reg(_REG_STATUS1)
        return bool(status & 0x20)

    @property
    def is_battery_present(self):
        """True if a battery is connected."""
        status = self._read_reg(_REG_STATUS1)
        return bool(status & 0x08)

    # ─── Voltage monitoring ──────────────────────────────────────

    @property
    def vbus_voltage(self):
        """VBUS (USB) voltage in millivolts."""
        data = self._read_regs(_REG_VBUS_H, 2)
        raw = (data[0] << 4) | (data[1] & 0x0F)
        return raw

    @property
    def system_voltage(self):
        """System rail voltage in millivolts."""
        data = self._read_regs(_REG_VSYS_H, 2)
        raw = (data[0] << 4) | (data[1] & 0x0F)
        return raw

    # ─── Charge control ─────────────────────────────────────────

    def enable_charging(self, enable=True):
        """Enable or disable battery charging."""
        if enable:
            self._set_bits(_REG_CHG_CTL, 0x01)
        else:
            self._clear_bits(_REG_CHG_CTL, 0x01)

    # ─── Power output control ────────────────────────────────────

    def enable_dcdc(self, channel, enable=True):
        """Enable/disable a DCDC converter (1-5)."""
        if not 1 <= channel <= 5:
            raise ValueError("DCDC channel must be 1-5")
        bit = 1 << (channel - 1)
        if enable:
            self._set_bits(_REG_DCDC_ONOFF, bit)
        else:
            self._clear_bits(_REG_DCDC_ONOFF, bit)

    def enable_aldo(self, channel, enable=True):
        """Enable/disable an ALDO LDO (1-4)."""
        if not 1 <= channel <= 4:
            raise ValueError("ALDO channel must be 1-4")
        bit = 1 << (channel - 1)
        if enable:
            self._set_bits(_REG_LDO_ONOFF0, bit)
        else:
            self._clear_bits(_REG_LDO_ONOFF0, bit)

    # ─── Power off ───────────────────────────────────────────────

    def power_off(self):
        """Shut down the system via PMIC."""
        self._set_bits(_REG_PWROFF_EN, 0x01)

    # ─── Debug info ──────────────────────────────────────────────

    def status(self):
        """Print a summary of power status."""
        print(f"Battery:  {self.battery_percent}%")
        print(f"VBAT:     {self.battery_voltage} (raw ADC)")
        print(f"Charging: {self.is_charging}")
        print(f"USB:      {self.is_vbus_present}")
        print(f"Battery:  {'present' if self.is_battery_present else 'absent'}")
