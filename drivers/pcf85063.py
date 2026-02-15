"""
PCF85063A Real-Time Clock Driver for MicroPython
Battery-backed RTC for the Waveshare ESP32-S3-Touch-AMOLED-2.06

Usage:
    from drivers.pcf85063 import PCF85063
    from machine import I2C, Pin
    from board_config import *

    i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)
    rtc = PCF85063(i2c)

    # Set time: (year, month, day, weekday, hour, minute, second)
    rtc.datetime((2026, 2, 15, 6, 14, 30, 0))

    # Read time
    dt = rtc.datetime()
    print(f"{dt[0]}-{dt[1]:02d}-{dt[2]:02d} {dt[4]:02d}:{dt[5]:02d}:{dt[6]:02d}")
"""

import board_config as BOARD

# Register addresses
_REG_CTRL1    = 0x00
_REG_CTRL2    = 0x01
_REG_OFFSET   = 0x02
_REG_SECONDS  = 0x04
_REG_MINUTES  = 0x05
_REG_HOURS    = 0x06
_REG_DAYS     = 0x07
_REG_WEEKDAYS = 0x08
_REG_MONTHS   = 0x09
_REG_YEARS    = 0x0A


def _bcd2dec(bcd):
    return (bcd >> 4) * 10 + (bcd & 0x0F)

def _dec2bcd(dec):
    return ((dec // 10) << 4) | (dec % 10)


class PCF85063:
    """PCF85063A RTC driver."""

    def __init__(self, i2c, addr=None):
        self._i2c = i2c
        self._addr = addr or BOARD.RTC_ADDR

    def _read_reg(self, reg):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, 1)[0]

    def _read_regs(self, reg, count):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, count)

    def _write_reg(self, reg, value):
        self._i2c.writeto(self._addr, bytes([reg, value]))

    def datetime(self, dt=None):
        """Get or set the date/time.

        Args:
            dt: Tuple (year, month, day, weekday, hour, minute, second)
                weekday: 0=Sunday ... 6=Saturday
                year: full year (e.g. 2026, stored as offset from 2000)

        Returns:
            Tuple (year, month, day, weekday, hour, minute, second) if no arg
        """
        if dt is None:
            data = self._read_regs(_REG_SECONDS, 7)
            return (
                _bcd2dec(data[6]) + 2000,      # year
                _bcd2dec(data[5] & 0x1F),       # month
                _bcd2dec(data[3] & 0x3F),       # day
                data[4] & 0x07,                  # weekday
                _bcd2dec(data[2] & 0x3F),       # hour
                _bcd2dec(data[1] & 0x7F),       # minute
                _bcd2dec(data[0] & 0x7F),       # second
            )
        else:
            year, month, day, weekday, hour, minute, second = dt
            self._write_reg(_REG_SECONDS, _dec2bcd(second))
            self._write_reg(_REG_MINUTES, _dec2bcd(minute))
            self._write_reg(_REG_HOURS, _dec2bcd(hour))
            self._write_reg(_REG_DAYS, _dec2bcd(day))
            self._write_reg(_REG_WEEKDAYS, weekday)
            self._write_reg(_REG_MONTHS, _dec2bcd(month))
            self._write_reg(_REG_YEARS, _dec2bcd(year - 2000))

    def reset(self):
        """Software reset the RTC."""
        self._write_reg(_REG_CTRL1, 0x58)  # Software reset command
