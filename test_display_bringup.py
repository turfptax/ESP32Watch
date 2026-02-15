"""
Display bring-up test for CO5300 AMOLED on Waveshare ESP32-S3-Touch-AMOLED-2.06
Run each step individually to diagnose where things fail.

Usage:
    import test_display_bringup as tdb
    tdb.step1_check_power()      # Read AXP2101 rail states
    tdb.step2_enable_power()     # Enable display power rails
    tdb.step3_reset_display()    # Hardware reset the CO5300
    tdb.step4_init_display()     # Send init commands via SPI
    tdb.step5_fill_color()       # Try pushing pixels
    tdb.full_bringup()           # Run all steps in sequence
"""

import time
from machine import Pin, SPI, I2C
import board_config as BOARD

# ─── AXP2101 registers for power rail control ───────────────────
AXP_ADDR = BOARD.PMIC_ADDR  # 0x34

# LDO enable registers
AXP_LDO_ONOFF0 = 0x90   # ALDO1-4 enable bits [3:0]
AXP_LDO_ONOFF1 = 0x91   # BLDO1-2 enable bits [1:0], DLDO1-2 [3:2]

# LDO voltage registers
AXP_ALDO1_VOL = 0x92
AXP_ALDO2_VOL = 0x93
AXP_ALDO3_VOL = 0x94
AXP_ALDO4_VOL = 0x95
AXP_BLDO1_VOL = 0x96
AXP_BLDO2_VOL = 0x97

# DCDC enable
AXP_DCDC_ONOFF = 0x80

# Voltage calculation: value * 100 + 500 = mV (for ALDOs and BLDOs)
# So 0x1C = 28 → 28*100+500 = 3300mV = 3.3V
#    0x0D = 13 → 13*100+500 = 1800mV = 1.8V

# ─── TCA9554 GPIO expander ──────────────────────────────────────
TCA_ADDR = BOARD.EXPANDER_ADDR  # 0x40
TCA_INPUT    = 0x00
TCA_OUTPUT   = 0x01
TCA_POLARITY = 0x02
TCA_CONFIG   = 0x03  # 0=output, 1=input

i2c = None
spi = None
cs_pin = None
rst_pin = None


def _get_i2c():
    global i2c
    if i2c is None:
        i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
                  freq=BOARD.I2C_FREQ)
    return i2c


def _axp_read(reg):
    bus = _get_i2c()
    bus.writeto(AXP_ADDR, bytes([reg]))
    return bus.readfrom(AXP_ADDR, 1)[0]


def _axp_write(reg, val):
    bus = _get_i2c()
    bus.writeto(AXP_ADDR, bytes([reg, val]))


def _tca_read(reg):
    bus = _get_i2c()
    bus.writeto(TCA_ADDR, bytes([reg]))
    return bus.readfrom(TCA_ADDR, 1)[0]


def _tca_write(reg, val):
    bus = _get_i2c()
    bus.writeto(TCA_ADDR, bytes([reg, val]))


def _voltage_str(reg_val):
    """Convert AXP2101 LDO register value to voltage string."""
    mv = reg_val * 100 + 500
    return f"{mv}mV"


# ═════════════════════════════════════════════════════════════════
# STEP 1: Check current power rail states
# ═════════════════════════════════════════════════════════════════
def step1_check_power():
    """Read and display all AXP2101 power rail states."""
    print("\n=== Step 1: AXP2101 Power Rail Status ===")

    dcdc_en = _axp_read(AXP_DCDC_ONOFF)
    ldo_en0 = _axp_read(AXP_LDO_ONOFF0)
    ldo_en1 = _axp_read(AXP_LDO_ONOFF1)

    print(f"DCDC enable reg (0x80): 0x{dcdc_en:02X} = {dcdc_en:08b}")
    print(f"LDO  enable reg (0x90): 0x{ldo_en0:02X} = {ldo_en0:08b}")
    print(f"LDO  enable reg (0x91): 0x{ldo_en1:02X} = {ldo_en1:08b}")

    print("\nALDO outputs:")
    for i, reg in enumerate([AXP_ALDO1_VOL, AXP_ALDO2_VOL, AXP_ALDO3_VOL, AXP_ALDO4_VOL]):
        val = _axp_read(reg)
        enabled = "ON" if (ldo_en0 & (1 << i)) else "OFF"
        print(f"  ALDO{i+1}: {_voltage_str(val):>8s}  [{enabled}]  (reg 0x{reg:02X} = 0x{val:02X})")

    print("BLDO outputs:")
    for i, reg in enumerate([AXP_BLDO1_VOL, AXP_BLDO2_VOL]):
        val = _axp_read(reg)
        enabled = "ON" if (ldo_en1 & (1 << i)) else "OFF"
        print(f"  BLDO{i+1}: {_voltage_str(val):>8s}  [{enabled}]  (reg 0x{reg:02X} = 0x{val:02X})")

    print("\nTCA9554 GPIO expander:")
    tca_cfg = _tca_read(TCA_CONFIG)
    tca_out = _tca_read(TCA_OUTPUT)
    tca_inp = _tca_read(TCA_INPUT)
    print(f"  Config (I/O dir): 0x{tca_cfg:02X} = {tca_cfg:08b}  (0=out, 1=in)")
    print(f"  Output register:  0x{tca_out:02X} = {tca_out:08b}")
    print(f"  Input  register:  0x{tca_inp:02X} = {tca_inp:08b}")


# ═════════════════════════════════════════════════════════════════
# STEP 2: Enable display power rails
# ═════════════════════════════════════════════════════════════════
def step2_enable_power():
    """Enable AXP2101 power rails for the display."""
    print("\n=== Step 2: Enabling Display Power ===")

    # Enable all ALDO outputs (we don't know which one powers the display)
    # Set safe voltages: ALDO1-4 to 3.3V
    for i, reg in enumerate([AXP_ALDO1_VOL, AXP_ALDO2_VOL, AXP_ALDO3_VOL, AXP_ALDO4_VOL]):
        current = _axp_read(reg)
        print(f"  ALDO{i+1} current voltage: {_voltage_str(current)}")

    # Enable ALDO1-4 (set bits 0-3 of reg 0x90)
    ldo_en0 = _axp_read(AXP_LDO_ONOFF0)
    _axp_write(AXP_LDO_ONOFF0, ldo_en0 | 0x0F)
    print(f"  ALDO1-4 enabled (0x90: 0x{ldo_en0:02X} -> 0x{ldo_en0 | 0x0F:02X})")

    # Enable BLDO1-2 (set bits 0-1 of reg 0x91)
    ldo_en1 = _axp_read(AXP_LDO_ONOFF1)
    _axp_write(AXP_LDO_ONOFF1, ldo_en1 | 0x03)
    print(f"  BLDO1-2 enabled (0x91: 0x{ldo_en1:02X} -> 0x{ldo_en1 | 0x03:02X})")

    # Set TCA9554 all pins as outputs, all HIGH (enable everything)
    _tca_write(TCA_CONFIG, 0x00)   # All pins output
    _tca_write(TCA_OUTPUT, 0xFF)   # All pins HIGH
    print("  TCA9554: all pins set to output HIGH")

    time.sleep_ms(100)
    print("  Power rails stabilized (100ms)")


# ═════════════════════════════════════════════════════════════════
# STEP 3: Hardware reset the display
# ═════════════════════════════════════════════════════════════════
def step3_reset_display():
    """Toggle the CO5300 reset pin."""
    global rst_pin
    print("\n=== Step 3: Display Hardware Reset ===")

    rst_pin = Pin(BOARD.LCD_RESET, Pin.OUT)
    print(f"  Reset pin: GPIO{BOARD.LCD_RESET}")

    rst_pin(1)
    time.sleep_ms(10)
    print("  RST HIGH -> wait 10ms")

    rst_pin(0)
    time.sleep_ms(20)
    print("  RST LOW  -> wait 20ms")

    rst_pin(1)
    time.sleep_ms(200)
    print("  RST HIGH -> wait 200ms (display booting)")
    print("  Reset complete")


# ═════════════════════════════════════════════════════════════════
# STEP 4: Initialize display via SPI
# ═════════════════════════════════════════════════════════════════
def step4_init_display():
    """Send CO5300 initialization commands via SPI."""
    global spi, cs_pin
    print("\n=== Step 4: Display Init via SPI ===")

    cs_pin = Pin(BOARD.LCD_CS, Pin.OUT, value=1)

    # Start with lower speed for init, can increase later
    spi = SPI(1,
              baudrate=10_000_000,
              polarity=0,
              phase=0,
              sck=Pin(BOARD.LCD_SCLK),
              mosi=Pin(BOARD.LCD_SDIO0))

    print(f"  SPI: sck=GPIO{BOARD.LCD_SCLK}, mosi=GPIO{BOARD.LCD_SDIO0}, cs=GPIO{BOARD.LCD_CS}")
    print(f"  Baudrate: 10 MHz")

    def write_cmd(cmd):
        """Send command in CO5300 QSPI-compatible format."""
        cs_pin(0)
        # Format: [0x02 instruction] [0x00] [cmd] [0x00]
        spi.write(bytes([0x02, 0x00, cmd, 0x00]))
        cs_pin(1)

    def write_cmd_data(cmd, data):
        """Send command + data byte."""
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, cmd, 0x00, data]))
        cs_pin(1)

    def write_cmd_data_multi(cmd, data_bytes):
        """Send command + multiple data bytes."""
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, cmd, 0x00]) + data_bytes)
        cs_pin(1)

    # ── Init sequence (from Arduino_GFX CO5300 driver) ──
    print("  Sending SLPOUT (0x11)...")
    write_cmd(0x11)  # Sleep out
    time.sleep_ms(120)

    print("  Sending extended command access (0xFE=0x00)...")
    write_cmd_data(0xFE, 0x00)

    print("  Sending SPI mode control (0xC4=0x80)...")
    write_cmd_data(0xC4, 0x80)  # QSPI mode (keep as QSPI since we're emulating it)

    print("  Sending pixel format (0x3A=0x55 = RGB565)...")
    write_cmd_data(0x3A, 0x55)

    print("  Sending write control display (0x53=0x20)...")
    write_cmd_data(0x53, 0x20)

    print("  Sending HBM brightness max (0x63=0xFF)...")
    write_cmd_data(0x63, 0xFF)

    print("  Sending DISPON (0x29)...")
    write_cmd(0x29)  # Display ON

    print("  Sending brightness (0x51=0xFF)...")
    write_cmd_data(0x51, 0xFF)  # Max brightness

    print("  Sending contrast off (0x58=0x00)...")
    write_cmd_data(0x58, 0x00)

    time.sleep_ms(50)
    print("  Init sequence complete!")
    print("  (Display should now be powered on — may show noise/white/black)")


# ═════════════════════════════════════════════════════════════════
# STEP 5: Try pushing pixel data
# ═════════════════════════════════════════════════════════════════
def step5_fill_color(color=0xF800):
    """Try to fill the screen with a solid color (default: red).

    Args:
        color: RGB565 color value (0xF800=red, 0x07E0=green, 0x001F=blue,
               0xFFFF=white, 0x0000=black)
    """
    global spi, cs_pin
    print(f"\n=== Step 5: Fill Screen with 0x{color:04X} ===")

    if spi is None or cs_pin is None:
        print("  ERROR: Run step4_init_display() first!")
        return

    W = BOARD.LCD_WIDTH   # 410
    H = BOARD.LCD_HEIGHT  # 502

    def write_cmd_data_multi(cmd, data_bytes):
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, cmd, 0x00]) + data_bytes)
        cs_pin(1)

    import struct

    # Set column address (0 to W-1)
    print(f"  CASET: 0 to {W-1}")
    write_cmd_data_multi(0x2A, struct.pack('>HH', 0, W - 1))

    # Set row address (0 to H-1)
    print(f"  RASET: 0 to {H-1}")
    write_cmd_data_multi(0x2B, struct.pack('>HH', 0, H - 1))

    # Send RAMWR command
    print(f"  RAMWR: sending {W}x{H} = {W*H} pixels...")

    # Create a row of pixels
    color_hi = color >> 8
    color_lo = color & 0xFF
    row = bytes([color_hi, color_lo] * W)  # One row = W*2 bytes

    # Start RAMWR
    cs_pin(0)
    spi.write(bytes([0x02, 0x00, 0x2C, 0x00]))  # RAMWR command

    # Send row by row
    for y in range(H):
        spi.write(row)
        if y % 100 == 0:
            print(f"    row {y}/{H}...")

    cs_pin(1)
    print(f"  Done! Screen should be filled with color 0x{color:04X}")


# ═════════════════════════════════════════════════════════════════
# STEP 6: Find the correct column/row offset
# ═════════════════════════════════════════════════════════════════
def step6_find_offset():
    """Draw colored borders to find the exact display offset.
    Look at the screen and report which edges are visible/cut off.
    """
    global spi, cs_pin
    print("\n=== Step 6: Display Offset Diagnostic ===")

    if spi is None or cs_pin is None:
        print("  ERROR: Run step4_init_display() first!")
        return

    import struct
    W = BOARD.LCD_WIDTH
    H = BOARD.LCD_HEIGHT

    def write_cmd_data_multi(cmd, data_bytes):
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, cmd, 0x00]) + data_bytes)
        cs_pin(1)

    def fill_region(x, y, w, h, color):
        """Fill a rectangular region with a color."""
        write_cmd_data_multi(0x2A, struct.pack('>HH', x, x + w - 1))
        write_cmd_data_multi(0x2B, struct.pack('>HH', y, y + h - 1))
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, 0x2C, 0x00]))
        row = bytes([color >> 8, color & 0xFF] * w)
        for _ in range(h):
            spi.write(row)
        cs_pin(1)

    # Fill entire area black first
    print("  Filling black...")
    fill_region(0, 0, W, H, 0x0000)

    # Draw colored borders (10px thick)
    B = 10
    print("  Drawing borders: RED=top, GREEN=bottom, BLUE=left, YELLOW=right")
    fill_region(0, 0, W, B, 0xF800)        # RED = top
    fill_region(0, H - B, W, B, 0x07E0)    # GREEN = bottom
    fill_region(0, 0, B, H, 0x001F)        # BLUE = left
    fill_region(W - B, 0, B, H, 0xFFE0)    # YELLOW = right

    # Draw a white crosshair in the center
    cx, cy = W // 2, H // 2
    fill_region(cx - 1, cy - 20, 2, 40, 0xFFFF)
    fill_region(cx - 20, cy - 1, 40, 2, 0xFFFF)

    # Draw corner markers with pixel position labels
    # Top-left white square
    fill_region(0, 0, 20, 20, 0xFFFF)
    # Bottom-right white square
    fill_region(W - 20, H - 20, 20, 20, 0xFFFF)

    print(f"  Window: CASET 0-{W-1}, RASET 0-{H-1}")
    print()
    print("  Look at the screen and report:")
    print("  - Can you see ALL 4 colored borders?")
    print("  - RED=top, GREEN=bottom, BLUE=left, YELLOW=right")
    print("  - Is the green bar from before still visible on any edge?")
    print("  - Are any borders cut off or shifted?")


def step6b_test_offset(col_offset=0, row_offset=0):
    """Try a specific column/row offset and fill with a test pattern.

    Args:
        col_offset: Pixels to shift right (try 0, 10, 20, 30)
        row_offset: Pixels to shift down (try 0, 1, 2)
    """
    global spi, cs_pin
    print(f"\n=== Testing offset: col={col_offset}, row={row_offset} ===")

    if spi is None or cs_pin is None:
        print("  ERROR: Run step4_init_display() first!")
        return

    import struct
    W = BOARD.LCD_WIDTH
    H = BOARD.LCD_HEIGHT

    def write_cmd_data_multi(cmd, data_bytes):
        cs_pin(0)
        spi.write(bytes([0x02, 0x00, cmd, 0x00]) + data_bytes)
        cs_pin(1)

    x0 = col_offset
    y0 = row_offset
    x1 = x0 + W - 1
    y1 = y0 + H - 1

    print(f"  CASET: {x0} to {x1}")
    print(f"  RASET: {y0} to {y1}")

    write_cmd_data_multi(0x2A, struct.pack('>HH', x0, x1))
    write_cmd_data_multi(0x2B, struct.pack('>HH', y0, y1))

    # Fill with red
    cs_pin(0)
    spi.write(bytes([0x02, 0x00, 0x2C, 0x00]))
    row = bytes([0xF8, 0x00] * W)
    for _ in range(H):
        spi.write(row)
    cs_pin(1)

    print(f"  Filled red with offset ({col_offset}, {row_offset})")
    print("  Is the green bar gone? Are all edges clean?")


# ═════════════════════════════════════════════════════════════════
# Full bring-up sequence
# ═════════════════════════════════════════════════════════════════
def full_bringup():
    """Run the complete display bring-up sequence."""
    print("=" * 50)
    print("  CO5300 AMOLED Display Bring-Up")
    print("=" * 50)

    step1_check_power()
    step2_enable_power()
    step3_reset_display()
    step4_init_display()
    step5_fill_color(0xF800)  # Red

    print("\n" + "=" * 50)
    print("  Bring-up complete!")
    print("  If screen is still black, try:")
    print("    tdb.step5_fill_color(0xFFFF)  # white")
    print("    tdb.step5_fill_color(0x07E0)  # green")
    print("=" * 50)
