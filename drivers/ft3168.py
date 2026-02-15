"""
FT3168 Capacitive Touch Controller Driver for MicroPython
I2C-based touch input for the Waveshare ESP32-S3-Touch-AMOLED-2.06

The FT3168 is part of the FocalTech FT6x36 family and uses the same
register map. Supports up to 2 simultaneous touch points.

Usage:
    from drivers.ft3168 import FT3168
    from board_config import *
    from machine import I2C, Pin

    i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=I2C_FREQ)
    touch = FT3168(i2c)
    touch.init()

    while True:
        points = touch.read()
        if points:
            for x, y, event in points:
                print(f"Touch at ({x}, {y}) event={event}")
"""

import time
from machine import Pin
import board_config as BOARD


# ─── FT3168 Register Map ────────────────────────────────────────
_REG_DEVICE_MODE   = 0x00
_REG_GEST_ID       = 0x01
_REG_TD_STATUS     = 0x02  # Number of touch points

# Touch point 1
_REG_P1_XH         = 0x03  # [7:6]=event, [3:0]=X high 4 bits
_REG_P1_XL         = 0x04  # X low 8 bits
_REG_P1_YH         = 0x05  # [7:4]=touch ID, [3:0]=Y high 4 bits
_REG_P1_YL         = 0x06  # Y low 8 bits
_REG_P1_WEIGHT     = 0x07
_REG_P1_MISC       = 0x08

# Touch point 2
_REG_P2_XH         = 0x09
_REG_P2_XL         = 0x0A
_REG_P2_YH         = 0x0B
_REG_P2_YL         = 0x0C
_REG_P2_WEIGHT     = 0x0D
_REG_P2_MISC       = 0x0E

# Configuration
_REG_TH_GROUP      = 0x80  # Touch threshold (0-255)
_REG_PERIODACTIVE  = 0x88  # Active period (3-14, default 12)
_REG_PERIODMONITOR = 0x89  # Monitor period (default 40)
_REG_LIB_VER_H     = 0xA1
_REG_LIB_VER_L     = 0xA2
_REG_CHIP_ID       = 0xA3
_REG_G_MODE        = 0xA4  # Interrupt mode
_REG_PWR_MODE      = 0xA5  # Power mode
_REG_FIRMID        = 0xA6
_REG_FOCALTECH_ID  = 0xA8
_REG_STATE         = 0xBC

# Event flags
EVENT_PRESS_DOWN   = 0x00
EVENT_LIFT_UP      = 0x01
EVENT_CONTACT      = 0x02
EVENT_NO_EVENT     = 0x03

# Gesture IDs
GESTURE_NONE       = 0x00
GESTURE_MOVE_UP    = 0x10
GESTURE_MOVE_LEFT  = 0x14
GESTURE_MOVE_DOWN  = 0x18
GESTURE_MOVE_RIGHT = 0x1C
GESTURE_ZOOM_IN    = 0x48
GESTURE_ZOOM_OUT   = 0x49


class FT3168:
    """FT3168 capacitive touch controller driver."""

    def __init__(self, i2c, addr=None, int_pin=None, rst_pin=None):
        """
        Args:
            i2c:      machine.I2C instance (pre-configured)
            addr:     I2C address (default 0x38)
            int_pin:  Interrupt GPIO number (default from board_config)
            rst_pin:  Reset GPIO number (default from board_config)
        """
        self._i2c = i2c
        self._addr = addr or BOARD.TOUCH_ADDR

        # Interrupt pin (active low, triggers on new touch data)
        int_num = int_pin or BOARD.TP_INT
        self._int = Pin(int_num, Pin.IN)

        # Reset pin
        rst_num = rst_pin or BOARD.TP_RESET
        self._rst = Pin(rst_num, Pin.OUT, value=1)

        # Read buffer (14 bytes covers both touch points)
        self._buf = bytearray(14)

        # Touch callback
        self._callback = None

    def init(self):
        """Initialize the touch controller with hardware reset."""
        # Hardware reset
        self._rst(0)
        time.sleep_ms(10)
        self._rst(1)
        time.sleep_ms(300)

        # Verify the chip responds
        chip_id = self._read_reg(_REG_CHIP_ID)
        # FT3168 should return its chip ID (varies by revision)
        print(f"FT3168 chip ID: 0x{chip_id:02X}")

        vendor_id = self._read_reg(_REG_FOCALTECH_ID)
        print(f"FT3168 vendor ID: 0x{vendor_id:02X}")

        # Set operating mode
        self._write_reg(_REG_DEVICE_MODE, 0x00)  # Normal operating mode

        # Set touch threshold
        self._write_reg(_REG_TH_GROUP, 22)  # Sensitivity (lower = more sensitive)

        # Set interrupt mode: trigger on touch
        self._write_reg(_REG_G_MODE, 0x00)  # Polling mode

    # ─── I2C helpers ─────────────────────────────────────────────

    def _read_reg(self, reg):
        """Read a single register byte."""
        self._i2c.writeto(self._addr, bytes([reg]))
        data = self._i2c.readfrom(self._addr, 1)
        return data[0]

    def _read_regs(self, reg, count):
        """Read multiple consecutive registers."""
        self._i2c.writeto(self._addr, bytes([reg]))
        return self._i2c.readfrom(self._addr, count)

    def _write_reg(self, reg, value):
        """Write a single register byte."""
        self._i2c.writeto(self._addr, bytes([reg, value]))

    # ─── Touch reading ───────────────────────────────────────────

    @property
    def touched(self):
        """Return True if screen is currently being touched."""
        return not self._int()  # Active low

    def read(self):
        """Read current touch points.

        Returns:
            List of (x, y, event) tuples, or empty list if no touch.
            event: EVENT_PRESS_DOWN, EVENT_LIFT_UP, EVENT_CONTACT
        """
        data = self._read_regs(_REG_TD_STATUS, 13)
        num_points = data[0] & 0x0F

        if num_points == 0 or num_points > 2:
            return []

        points = []
        for i in range(num_points):
            offset = 1 + i * 6  # Each point takes 6 bytes starting at offset 1
            event = (data[offset] >> 6) & 0x03
            x = ((data[offset] & 0x0F) << 8) | data[offset + 1]
            y = ((data[offset + 2] & 0x0F) << 8) | data[offset + 3]
            points.append((x, y, event))

        return points

    def read_gesture(self):
        """Read the current gesture ID.

        Returns:
            Gesture constant (GESTURE_NONE, GESTURE_MOVE_UP, etc.)
        """
        return self._read_reg(_REG_GEST_ID)

    # ─── Interrupt-driven touch ──────────────────────────────────

    def on_touch(self, callback):
        """Register a callback for touch events.
        callback receives a list of (x, y, event) tuples.

        Args:
            callback: Function(points_list) or None to disable.
        """
        self._callback = callback
        if callback:
            self._int.irq(
                trigger=Pin.IRQ_FALLING,
                handler=self._irq_handler
            )
        else:
            self._int.irq(handler=None)

    def _irq_handler(self, pin):
        """ISR for touch interrupt — schedule callback."""
        if self._callback:
            import micropython
            micropython.schedule(self._scheduled_read, None)

    def _scheduled_read(self, _):
        """Read touch data outside ISR context."""
        points = self.read()
        if points and self._callback:
            self._callback(points)
