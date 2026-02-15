"""
Touch controller diagnostic for Waveshare ESP32-S3-Touch-AMOLED-2.06
Run from REPL:
    import test_touch as tt
    tt.diag()           # Full diagnostic
    tt.monitor()        # Live touch monitor (Ctrl+C to stop)
"""

import time
from machine import I2C, Pin
import board_config as BOARD


def _get_i2c():
    return I2C(0, sda=Pin(BOARD.I2C_SDA),
               scl=Pin(BOARD.I2C_SCL),
               freq=BOARD.I2C_FREQ)


def _tca9554_enable_all(i2c):
    """Set TCA9554 all outputs HIGH (ensures touch power enabled)."""
    addr = BOARD.EXPANDER_ADDR  # 0x40
    try:
        # Register 3: config — 0x00 = all outputs
        i2c.writeto(addr, bytes([0x03, 0x00]))
        # Register 1: output — 0xFF = all HIGH
        i2c.writeto(addr, bytes([0x01, 0xFF]))
        print("TCA9554: all outputs set HIGH")
    except Exception as e:
        print(f"TCA9554 error: {e}")


def _reset_touch():
    """Hardware reset the touch controller."""
    rst = Pin(BOARD.TP_RESET, Pin.OUT)
    rst(0)
    time.sleep_ms(20)
    rst(1)
    time.sleep_ms(300)
    print("Touch controller reset complete")


def diag():
    """Full touch controller diagnostic — dump all ID registers."""
    i2c = _get_i2c()
    addr = BOARD.TOUCH_ADDR  # 0x18

    print("=" * 50)
    print("TOUCH CONTROLLER DIAGNOSTIC")
    print("=" * 50)

    # Step 1: Verify I2C presence
    devices = i2c.scan()
    if addr in devices:
        print(f"\n[OK] Touch controller found at 0x{addr:02X}")
    else:
        print(f"\n[FAIL] Touch NOT found at 0x{addr:02X}")
        print(f"  Devices on bus: {['0x%02X' % d for d in devices]}")
        return

    # Step 2: Enable power via TCA9554
    print("\n--- Enabling TCA9554 outputs ---")
    _tca9554_enable_all(i2c)
    time.sleep_ms(100)

    # Step 3: Hardware reset
    print("\n--- Hardware reset ---")
    _reset_touch()

    # Step 4: Check interrupt pin
    int_pin = Pin(BOARD.TP_INT, Pin.IN)
    print(f"\nINT pin (GPIO{BOARD.TP_INT}) state: {'LOW (touch active?)' if not int_pin() else 'HIGH (idle)'}")

    # Step 5: Dump ID registers — try both FocalTech and Hynitron locations
    print("\n--- Register dump ---")

    # Common FocalTech registers
    ft_regs = {
        0x00: "DEVICE_MODE",
        0x01: "GEST_ID",
        0x02: "TD_STATUS (num touches)",
        0xA3: "CHIP_ID (FocalTech)",
        0xA6: "FIRMWARE_ID",
        0xA8: "FOCALTECH_ID",
        0xA1: "LIB_VER_H",
        0xA2: "LIB_VER_L",
    }

    # Hynitron CST816 registers (in case it's a CST chip)
    cst_regs = {
        0xA7: "CHIP_ID (CST816)",
        0xA9: "PROJECT_ID (CST816)",
    }

    all_regs = {**ft_regs, **cst_regs}

    for reg, name in sorted(all_regs.items()):
        try:
            i2c.writeto(addr, bytes([reg]))
            val = i2c.readfrom(addr, 1)[0]
            print(f"  Reg 0x{reg:02X} ({name:25s}) = 0x{val:02X} ({val})")
        except Exception as e:
            print(f"  Reg 0x{reg:02X} ({name:25s}) = ERROR: {e}")

    # Step 6: Try reading a block of touch data
    print("\n--- Raw touch data (regs 0x00-0x0F) ---")
    try:
        i2c.writeto(addr, bytes([0x00]))
        data = i2c.readfrom(addr, 16)
        for i in range(0, 16, 8):
            hex_str = " ".join(f"{b:02X}" for b in data[i:i + 8])
            print(f"  0x{i:02X}: {hex_str}")
    except Exception as e:
        print(f"  Error: {e}")

    # Step 7: Brute-force register scan for non-zero values
    print("\n--- Non-zero registers (0x00-0xFF) ---")
    found = []
    for reg in range(0x100):
        try:
            i2c.writeto(addr, bytes([reg]))
            val = i2c.readfrom(addr, 1)[0]
            if val != 0x00:
                found.append((reg, val))
        except:
            pass

    if found:
        for reg, val in found:
            print(f"  Reg 0x{reg:02X} = 0x{val:02X} ({val})")
    else:
        print("  All registers read 0x00!")
        print("  -> Touch controller may not be properly initialized")
        print("  -> Try touching the screen and running diag() again")

    print("\n--- Interrupt test ---")
    print("Touch the screen now...")
    for i in range(30):  # 3 seconds
        if not int_pin():
            print(f"  INT triggered at {i * 100}ms!")
            # Read touch data immediately
            try:
                i2c.writeto(addr, bytes([0x02]))
                data = i2c.readfrom(addr, 7)
                num = data[0] & 0x0F
                print(f"  Touches: {num}")
                if num > 0:
                    evt = (data[1] >> 6) & 0x03
                    x = ((data[1] & 0x0F) << 8) | data[2]
                    y = ((data[3] & 0x0F) << 8) | data[4]
                    print(f"  Point 1: x={x}, y={y}, event={evt}")
            except Exception as e:
                print(f"  Read error: {e}")
            break
        time.sleep_ms(100)
    else:
        print("  No INT trigger detected in 3 seconds")

    print("\n" + "=" * 50)


def monitor():
    """Live touch monitor — shows touch events in real time.
    Touch the screen to see coordinates.
    Press Ctrl+C to stop.
    """
    i2c = _get_i2c()
    addr = BOARD.TOUCH_ADDR
    int_pin = Pin(BOARD.TP_INT, Pin.IN)

    # Make sure touch is powered and reset
    _tca9554_enable_all(i2c)
    _reset_touch()

    events = {0: "DOWN", 1: "UP", 2: "CONTACT", 3: "NONE"}

    print("\nTouch monitor active — touch the screen! (Ctrl+C to stop)")
    print("-" * 40)

    try:
        while True:
            if not int_pin():  # INT is active low
                try:
                    i2c.writeto(addr, bytes([0x02]))
                    data = i2c.readfrom(addr, 7)
                    num = data[0] & 0x0F
                    if 0 < num <= 2:
                        evt = (data[1] >> 6) & 0x03
                        x = ((data[1] & 0x0F) << 8) | data[2]
                        y = ((data[3] & 0x0F) << 8) | data[4]
                        evt_name = events.get(evt, f"?{evt}")
                        print(f"  Touch: ({x:3d}, {y:3d})  event={evt_name}")
                except:
                    pass
                time.sleep_ms(50)
            else:
                time.sleep_ms(20)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
