"""
QMI8658 6-Axis IMU Driver for MicroPython (Accelerometer only)
Provides motion detection for wake-on-motion on the
Waveshare ESP32-S3-Touch-AMOLED-2.06.

The gyroscope is left disabled to save power — only the accelerometer
is used for detecting wrist raises and significant motion.

Usage:
    from drivers.qmi8658 import QMI8658
    from machine import I2C, Pin
    from board_config import *

    i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)
    imu = QMI8658(i2c)
    imu.init()

    print(imu.read_accel())       # (ax, ay, az) raw int16
    print(imu.detect_motion())    # True if significant movement
"""

import struct
import board_config as BOARD


# ─── QMI8658 Register Map ──────────────────────────────────────
_REG_WHO_AM_I    = 0x00  # Chip ID (should return 0x05)
_REG_REVISION    = 0x01  # Revision ID
_REG_CTRL1       = 0x02  # SPI/I2C config, INT pin mode
_REG_CTRL2       = 0x03  # Accelerometer config (range + ODR)
_REG_CTRL3       = 0x04  # Gyroscope config
_REG_CTRL5       = 0x06  # Low-pass filter config
_REG_CTRL7       = 0x08  # Sensor enable (accel + gyro on/off)
_REG_CTRL8       = 0x09  # Motion detection control
_REG_CTRL9       = 0x0A  # Host command register

_REG_STATUSINT   = 0x2D  # Sensor data available status
_REG_STATUS0     = 0x2E  # Output data overrun/available

# Accelerometer data registers (little-endian: low byte first)
_REG_AX_L        = 0x35
_REG_AX_H        = 0x36
_REG_AY_L        = 0x37
_REG_AY_H        = 0x38
_REG_AZ_L        = 0x39
_REG_AZ_H        = 0x3A

# Temperature
_REG_TEMP_L      = 0x33
_REG_TEMP_H      = 0x34

# CTRL2 accelerometer range bits [6:4]
_ACC_RANGE_2G    = 0x00
_ACC_RANGE_4G    = 0x10
_ACC_RANGE_8G    = 0x20
_ACC_RANGE_16G   = 0x30

# CTRL2 accelerometer ODR bits [3:0]
_ACC_ODR_8000    = 0x00
_ACC_ODR_4000    = 0x01
_ACC_ODR_2000    = 0x02
_ACC_ODR_1000    = 0x03
_ACC_ODR_500     = 0x04
_ACC_ODR_250     = 0x05
_ACC_ODR_125     = 0x06
_ACC_ODR_62      = 0x07
_ACC_ODR_31      = 0x08

# CTRL7 sensor enable bits
_CTRL7_ACC_EN    = 0x01
_CTRL7_GYR_EN    = 0x02

# Expected chip ID
_CHIP_ID         = 0x05


class QMI8658:
    """QMI8658 IMU driver — accelerometer-only for motion detection."""

    def __init__(self, i2c, addr=None):
        self._i2c = i2c
        self._addr = addr or BOARD.IMU_ADDR
        self._last_accel = (0, 0, 0)

    def init(self):
        """Initialize the accelerometer at 4G range, ~31Hz ODR."""
        chip_id = self._read_reg(_REG_WHO_AM_I)
        if chip_id != _CHIP_ID:
            raise RuntimeError(
                f"QMI8658 not found: WHO_AM_I=0x{chip_id:02X} (expected 0x{_CHIP_ID:02X})"
            )
        print(f"QMI8658 IMU detected (rev 0x{self._read_reg(_REG_REVISION):02X})")

        # Disable all sensors before configuring
        self._write_reg(_REG_CTRL7, 0x00)

        # Configure accelerometer: 4G range, 31.25 Hz ODR (low power)
        self._write_reg(_REG_CTRL2, _ACC_RANGE_4G | _ACC_ODR_31)

        # Low-pass filter: enable for accel (default settings)
        self._write_reg(_REG_CTRL5, 0x01)

        # Enable accelerometer only (gyro stays off)
        self._write_reg(_REG_CTRL7, _CTRL7_ACC_EN)

        # Seed the last-accel reading for motion detection
        self._last_accel = self.read_accel()

    # ─── I2C helpers ─────────────────────────────────────────────

    def _read_reg(self, reg):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, 1)[0]

    def _read_regs(self, reg, count):
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, count)

    def _write_reg(self, reg, value):
        self._i2c.writeto(self._addr, bytes([reg, value]))

    # ─── Accelerometer ───────────────────────────────────────────

    def read_accel(self):
        """Read accelerometer XYZ. Returns (ax, ay, az) as signed int16.

        At 4G range: 1 LSB ~ 0.122 mg (sensitivity 8192 LSB/g).
        Gravity reads as ~8192 on the downward axis.
        """
        data = self._read_regs(_REG_AX_L, 6)
        ax = struct.unpack_from('<h', data, 0)[0]
        ay = struct.unpack_from('<h', data, 2)[0]
        az = struct.unpack_from('<h', data, 4)[0]
        return (ax, ay, az)

    def detect_motion(self, threshold=3000):
        """Check if significant motion occurred since last call.

        Compares current accel reading to the previous one.
        threshold=3000 at 4G range is ~0.37g of change — enough to
        detect a wrist raise but not tiny vibrations.

        Returns True if motion exceeds threshold.
        """
        ax, ay, az = self.read_accel()
        lx, ly, lz = self._last_accel
        delta = abs(ax - lx) + abs(ay - ly) + abs(az - lz)
        self._last_accel = (ax, ay, az)
        return delta > threshold

    def read_temperature(self):
        """Read chip temperature in degrees C (approximate)."""
        data = self._read_regs(_REG_TEMP_L, 2)
        raw = struct.unpack_from('<h', data, 0)[0]
        return raw / 256.0

    # ─── Power control ───────────────────────────────────────────

    def standby(self):
        """Disable accelerometer (low power standby)."""
        self._write_reg(_REG_CTRL7, 0x00)

    def resume(self):
        """Re-enable accelerometer after standby."""
        self._write_reg(_REG_CTRL7, _CTRL7_ACC_EN)
