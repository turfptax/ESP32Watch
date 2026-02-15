"""
CO5300 AMOLED Display Driver for MicroPython (ESP32-S3)
Drives the 410x502 AMOLED via single-lane SPI on the QSPI bus.

The CO5300 QSPI protocol sends instruction/address on 1 line and data on
4 lines. Since MicroPython only supports standard SPI, we send everything
on a single line (SDIO0). The CO5300 accepts this — pixel pushing is ~4x
slower than native QSPI but fully functional for UI applications.

Protocol format for each transaction:
    CS LOW → [0x02] [0x00] [cmd] [0x00] [data...] → CS HIGH

Confirmed working settings (Waveshare ESP32-S3-Touch-AMOLED-2.06):
    SPI1, 10 MHz, sck=GPIO11, mosi=GPIO4, cs=GPIO12, rst=GPIO8
    Column offset = 20, Row offset = 0

Usage:
    from drivers.co5300 import CO5300
    import board_config as BOARD

    display = CO5300()
    display.init()
    display.fill(BOARD.COLOR_BLACK)
    display.text("Hello!", 10, 10, BOARD.COLOR_WHITE)
    display.show()
"""

import time
import struct
from machine import Pin, SPI
import framebuf

import board_config as BOARD


# ─── CO5300 Register Definitions ────────────────────────────────
_SLPIN      = 0x10
_SLPOUT     = 0x11
_INVOFF     = 0x20
_INVON      = 0x21
_DISPOFF    = 0x28
_DISPON     = 0x29
_CASET      = 0x2A  # Column address set
_RASET      = 0x2B  # Row address set
_RAMWR      = 0x2C  # Memory write
_MADCTL     = 0x36  # Memory access control
_PIXFMT     = 0x3A  # Pixel format
_SPIMODECTL = 0xC4  # SPI mode control
_BRIGHTNESS = 0x51  # Write brightness value
_WCTRLD1    = 0x53  # Write control display 1
_HBM_BRIGHT = 0x63  # HBM brightness
_WCE        = 0x58  # Write contrast enhancement

# Pixel format
_PIXFMT_16BIT = 0x55  # RGB565


class CO5300:
    """
    CO5300 AMOLED display driver using single-lane SPI.
    Provides a framebuffer-based drawing interface.
    """

    def __init__(self, width=None, height=None, rotation=0,
                 spi_id=1, baudrate=10_000_000,
                 cs=None, sclk=None, mosi=None, rst=None,
                 col_offset=None, row_offset=None):
        """
        Args:
            width:      Display width (default from board_config)
            height:     Display height (default from board_config)
            rotation:   0, 1, 2, or 3 (90 degree increments)
            spi_id:     SPI peripheral ID (1 or 2)
            baudrate:   SPI clock speed in Hz (10 MHz confirmed working)
            cs:         Chip select pin number
            sclk:       SPI clock pin number
            mosi:       SPI MOSI pin number (uses SDIO0 for single-lane SPI)
            rst:        Reset pin number
            col_offset: Column offset in controller RAM (default from board_config)
            row_offset: Row offset in controller RAM (default from board_config)
        """
        self.width = width or BOARD.LCD_WIDTH
        self.height = height or BOARD.LCD_HEIGHT
        self._rotation = rotation
        self._col_offset = col_offset if col_offset is not None else BOARD.LCD_COL_OFFSET
        self._row_offset = row_offset if row_offset is not None else BOARD.LCD_ROW_OFFSET

        # Pin setup
        self._cs = Pin(cs or BOARD.LCD_CS, Pin.OUT, value=1)
        self._rst = Pin(rst or BOARD.LCD_RESET, Pin.OUT, value=1)

        # SPI bus — use SDIO0 as MOSI for single-lane mode
        self._spi = SPI(
            spi_id,
            baudrate=baudrate,
            polarity=0,
            phase=0,
            sck=Pin(sclk or BOARD.LCD_SCLK),
            mosi=Pin(mosi or BOARD.LCD_SDIO0),
        )

        # Framebuffer for drawing operations
        # RGB565 = 2 bytes/pixel. 410x502 = ~401 KB (fits in 8MB PSRAM)
        self._buf = bytearray(self.width * self.height * 2)
        self.fb = framebuf.FrameBuffer(
            self._buf, self.width, self.height, framebuf.RGB565
        )

    # ─── Low-level SPI communication ────────────────────────────

    def _write_cmd(self, cmd):
        """Send a command byte in CO5300 QSPI-compatible format."""
        self._cs(0)
        self._spi.write(bytes([0x02, 0x00, cmd, 0x00]))
        self._cs(1)

    def _write_cmd_data(self, cmd, data):
        """Send a command followed by data byte(s)."""
        self._cs(0)
        if isinstance(data, int):
            self._spi.write(bytes([0x02, 0x00, cmd, 0x00, data]))
        else:
            self._spi.write(bytes([0x02, 0x00, cmd, 0x00]) + data)
        self._cs(1)

    # ─── Initialization ─────────────────────────────────────────

    def init(self):
        """Initialize the CO5300 display.
        Uses the confirmed working sequence from test_display_bringup.
        """
        # Hardware reset
        self._rst(1)
        time.sleep_ms(10)
        self._rst(0)
        time.sleep_ms(20)
        self._rst(1)
        time.sleep_ms(200)

        # Sleep out
        self._write_cmd(_SLPOUT)
        time.sleep_ms(120)

        # Enter user command set
        self._write_cmd_data(0xFE, 0x00)

        # SPI mode control: 0x80 = QSPI mode (we emulate it on single lane)
        self._write_cmd_data(_SPIMODECTL, 0x80)

        # Pixel format: 16-bit RGB565
        self._write_cmd_data(_PIXFMT, _PIXFMT_16BIT)

        # Write control display: enable brightness control
        self._write_cmd_data(_WCTRLD1, 0x20)

        # Set HBM brightness max
        self._write_cmd_data(_HBM_BRIGHT, 0xFF)

        # Display ON
        self._write_cmd(_DISPON)

        # Set brightness to max
        self._write_cmd_data(_BRIGHTNESS, 0xFF)

        # Contrast enhancement off
        self._write_cmd_data(_WCE, 0x00)

        time.sleep_ms(50)

        # Clear screen to black
        self.fill(BOARD.COLOR_BLACK)
        self.show()

    # ─── Pixel pushing ──────────────────────────────────────────

    def _set_window(self, x0, y0, x1, y1):
        """Set the active drawing window (applying col/row offsets)."""
        x0 += self._col_offset
        x1 += self._col_offset
        y0 += self._row_offset
        y1 += self._row_offset
        self._write_cmd_data(_CASET, struct.pack('>HH', x0, x1))
        self._write_cmd_data(_RASET, struct.pack('>HH', y0, y1))

    def show(self):
        """Push the entire framebuffer to the display."""
        self._set_window(0, 0, self.width - 1, self.height - 1)

        # Send pixel data in chunks
        CHUNK = 4096
        self._cs(0)
        self._spi.write(bytes([0x02, 0x00, _RAMWR, 0x00]))
        for i in range(0, len(self._buf), CHUNK):
            self._spi.write(self._buf[i:i + CHUNK])
        self._cs(1)

    def show_region(self, x, y, w, h):
        """Push a rectangular region of the framebuffer to the display.
        More efficient than full-screen refresh for small UI updates.
        """
        self._set_window(x, y, x + w - 1, y + h - 1)
        self._cs(0)
        self._spi.write(bytes([0x02, 0x00, _RAMWR, 0x00]))
        for row in range(y, y + h):
            start = (row * self.width + x) * 2
            self._spi.write(self._buf[start:start + w * 2])
        self._cs(1)

    # ─── Drawing API (framebuffer-backed) ────────────────────────

    def fill(self, color):
        """Fill entire display with a color."""
        self.fb.fill(color)

    def pixel(self, x, y, color):
        """Set a single pixel."""
        self.fb.pixel(x, y, color)

    def rect(self, x, y, w, h, color, fill=False):
        """Draw a rectangle (outline or filled)."""
        if fill:
            self.fb.fill_rect(x, y, w, h, color)
        else:
            self.fb.rect(x, y, w, h, color)

    def hline(self, x, y, w, color):
        self.fb.hline(x, y, w, color)

    def vline(self, x, y, h, color):
        self.fb.vline(x, y, h, color)

    def line(self, x0, y0, x1, y1, color):
        self.fb.line(x0, y0, x1, y1, color)

    def text(self, s, x, y, color, scale=1):
        """Draw text using the built-in 8x8 font.
        For scale > 1, each pixel is drawn as a scale x scale block.
        """
        if scale == 1:
            self.fb.text(s, x, y, color)
        else:
            tw = len(s) * 8
            th = 8
            tmp = bytearray(tw * th * 2)
            tmp_fb = framebuf.FrameBuffer(tmp, tw, th, framebuf.RGB565)
            tmp_fb.fill(0)
            tmp_fb.text(s, 0, 0, color)
            for py in range(th):
                for px in range(tw):
                    c = tmp_fb.pixel(px, py)
                    if c != 0:
                        self.fb.fill_rect(
                            x + px * scale, y + py * scale,
                            scale, scale, color
                        )

    def circle(self, cx, cy, r, color, fill=False):
        """Draw a circle using midpoint algorithm."""
        if fill:
            for dy in range(-r, r + 1):
                dx = int((r * r - dy * dy) ** 0.5)
                self.fb.hline(cx - dx, cy + dy, 2 * dx + 1, color)
        else:
            x = r
            y = 0
            err = 1 - r
            while x >= y:
                self.fb.pixel(cx + x, cy + y, color)
                self.fb.pixel(cx + y, cy + x, color)
                self.fb.pixel(cx - y, cy + x, color)
                self.fb.pixel(cx - x, cy + y, color)
                self.fb.pixel(cx - x, cy - y, color)
                self.fb.pixel(cx - y, cy - x, color)
                self.fb.pixel(cx + y, cy - x, color)
                self.fb.pixel(cx + x, cy - y, color)
                y += 1
                if err < 0:
                    err += 2 * y + 1
                else:
                    x -= 1
                    err += 2 * (y - x) + 1

    # ─── Display control ────────────────────────────────────────

    def brightness(self, level):
        """Set display brightness (0-255)."""
        self._write_cmd_data(_BRIGHTNESS, max(0, min(255, level)))

    def display_on(self):
        self._write_cmd(_DISPON)

    def display_off(self):
        self._write_cmd(_DISPOFF)

    def sleep(self):
        """Enter sleep mode (low power)."""
        self._write_cmd(_DISPOFF)
        time.sleep_ms(20)
        self._write_cmd(_SLPIN)
        time.sleep_ms(120)

    def wake(self):
        """Exit sleep mode."""
        self._write_cmd(_SLPOUT)
        time.sleep_ms(120)
        self._write_cmd(_DISPON)

    def invert(self, enable=True):
        """Invert display colors."""
        self._write_cmd(_INVON if enable else _INVOFF)

    def deinit(self):
        """Release SPI bus."""
        self._spi.deinit()
