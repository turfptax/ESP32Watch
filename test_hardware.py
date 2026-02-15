"""
Hardware test script — run this FIRST to validate each component.
Upload this file and run it from the REPL to test one piece at a time.

Usage (from MicroPython REPL):
    import test_hardware
    test_hardware.test_i2c()        # Check what's on the I2C bus
    test_hardware.test_display()    # Test the AMOLED screen
    test_hardware.test_touch()      # Test touch input
    test_hardware.test_pmic()       # Test battery/power info
    test_hardware.test_rtc()        # Test the real-time clock
    test_hardware.test_sdcard()     # Test micro SD card reader
    test_hardware.test_wifi()       # Test WiFi connectivity
    test_hardware.run_all()         # Run everything
"""

import time
import gc
from machine import I2C, Pin
import board_config as BOARD


def test_i2c():
    """Scan the I2C bus and report all devices found."""
    print("\n=== I2C Bus Scan ===")
    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)
    devices = i2c.scan()

    expected = {
        0x18: "FT3168 Touch",
        0x34: "AXP2101 PMIC",
        0x40: "TCA9554 GPIO Expander",
        0x51: "PCF85063 RTC",
        0x6B: "QMI8658 IMU",
        # 0x10: "ES8311 Audio"  — only appears after CODEC_EN (GPIO46) set HIGH
    }

    print(f"Found {len(devices)} device(s):")
    for addr in devices:
        name = expected.get(addr, "Unknown")
        print(f"  0x{addr:02X} - {name}")

    missing = set(expected.keys()) - set(devices)
    if missing:
        print(f"\nMissing expected devices:")
        for addr in missing:
            print(f"  0x{addr:02X} - {expected[addr]}")
    else:
        print("\nAll expected I2C devices found!")

    return i2c


def test_display():
    """Test the AMOLED display with colored rectangles."""
    print("\n=== Display Test ===")
    from drivers.co5300 import CO5300

    display = CO5300()
    print("Initializing CO5300...")
    display.init()

    print("Drawing test pattern...")
    W, H = display.width, display.height

    # Color bars
    colors = [
        (BOARD.COLOR_RED, "Red"),
        (BOARD.COLOR_GREEN, "Green"),
        (BOARD.COLOR_BLUE, "Blue"),
        (BOARD.COLOR_CYAN, "Cyan"),
        (BOARD.COLOR_MAGENTA, "Magenta"),
        (BOARD.COLOR_YELLOW, "Yellow"),
        (BOARD.COLOR_WHITE, "White"),
    ]

    bar_h = H // len(colors)
    for i, (color, name) in enumerate(colors):
        y = i * bar_h
        display.rect(0, y, W, bar_h, color, fill=True)
        display.text(name, 10, y + bar_h // 2 - 4, BOARD.COLOR_BLACK)

    print("Pushing to display...")
    display.show()
    print("Display test complete. You should see colored bars.")
    return display


def test_touch():
    """Test touch input — prints coordinates for 10 seconds."""
    print("\n=== Touch Test ===")
    print("Touch the screen within 10 seconds...")

    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)

    from drivers.ft3168 import FT3168
    touch = FT3168(i2c)
    touch.init()

    start = time.time()
    count = 0
    while time.time() - start < 10:
        if touch.touched:
            points = touch.read()
            for x, y, event in points:
                events = {0: "DOWN", 1: "UP", 2: "CONTACT", 3: "NONE"}
                print(f"  Touch: ({x}, {y}) event={events.get(event, event)}")
                count += 1
            time.sleep_ms(100)
        time.sleep_ms(20)

    print(f"Touch test complete. Detected {count} touch events.")


def test_pmic():
    """Test the AXP2101 power management IC."""
    print("\n=== PMIC Test ===")

    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)

    from drivers.axp2101 import AXP2101
    pmic = AXP2101(i2c)
    pmic.init()
    pmic.status()


def test_rtc():
    """Test the PCF85063 real-time clock."""
    print("\n=== RTC Test ===")

    i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
              freq=BOARD.I2C_FREQ)

    from drivers.pcf85063 import PCF85063
    rtc = PCF85063(i2c)

    dt = rtc.datetime()
    print(f"Current RTC time: {dt[0]}-{dt[1]:02d}-{dt[2]:02d} "
          f"{dt[4]:02d}:{dt[5]:02d}:{dt[6]:02d}")

    # Set a test time and read back
    print("Setting RTC to 2026-02-15 12:00:00...")
    rtc.datetime((2026, 2, 15, 0, 12, 0, 0))  # Sunday
    time.sleep(1)

    dt = rtc.datetime()
    print(f"RTC now reads: {dt[0]}-{dt[1]:02d}-{dt[2]:02d} "
          f"{dt[4]:02d}:{dt[5]:02d}:{dt[6]:02d}")
    print("RTC test complete.")


def test_wifi():
    """Test WiFi scanning (doesn't connect)."""
    print("\n=== WiFi Scan ===")
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    print("Scanning for networks...")
    networks = wlan.scan()
    print(f"Found {len(networks)} network(s):")
    for ssid, bssid, channel, rssi, authmode, hidden in networks:
        ssid_str = ssid.decode('utf-8') if ssid else "(hidden)"
        auth_names = {0: "Open", 1: "WEP", 2: "WPA-PSK",
                      3: "WPA2-PSK", 4: "WPA/WPA2-PSK"}
        auth = auth_names.get(authmode, f"Auth:{authmode}")
        print(f"  {ssid_str:30s} ch:{channel:2d} rssi:{rssi}dBm {auth}")

    wlan.active(False)
    print("WiFi scan complete.")


def _print_sd_info():
    """Print SD card info after successful mount."""
    import os
    files = os.listdir("/sd")
    print(f"Files on card: {files}")
    stat = os.statvfs("/sd")
    block_size = stat[0]
    total_blocks = stat[2]
    free_blocks = stat[3]
    total_mb = (block_size * total_blocks) / (1024 * 1024)
    free_mb = (block_size * free_blocks) / (1024 * 1024)
    print(f"Total: {total_mb:.1f} MB, Free: {free_mb:.1f} MB")


def test_sdcard():
    """Test the micro SD card reader."""
    print("\n=== SD Card Test ===")
    import os
    from machine import Pin

    # Board pins: CLK=GPIO2, CMD/MOSI=GPIO1, DATA/MISO=GPIO3, CS=GPIO17
    mounted = False

    # ── Method 1: machine.SDCard with SPI slot=3 (confirmed working) ──
    print("Method 1: machine.SDCard (hardware SPI slot=3)...")
    try:
        from machine import SDCard
        sd = SDCard(slot=3,
                    sck=Pin(BOARD.SD_CLK),
                    mosi=Pin(BOARD.SD_CMD),
                    miso=Pin(BOARD.SD_DATA),
                    cs=Pin(BOARD.SD_CS))
        os.mount(sd, "/sd")
        print("  Mounted!")
        mounted = True
    except Exception as e:
        print(f"  slot=3 failed: {e}")

    # ── Method 2: Pure-Python sdcard.py driver with machine.SPI ──
    if not mounted:
        print("Method 2: Pure-Python sdcard driver (software SPI)...")
        try:
            import sdcard
            from machine import SPI
            spi = SPI(2,
                      baudrate=1_000_000,
                      polarity=0,
                      phase=0,
                      sck=Pin(BOARD.SD_CLK),
                      mosi=Pin(BOARD.SD_CMD),
                      miso=Pin(BOARD.SD_DATA))
            cs = Pin(BOARD.SD_CS, Pin.OUT)
            sd = sdcard.SDCard(spi, cs)
            os.mount(sd, "/sd")
            print("  Mounted via sdcard.py driver!")
            mounted = True
        except ImportError:
            print("  sdcard.py not found — upload it for software SPI support.")
            print("  Get it from: github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py")
        except Exception as e3:
            print(f"  Software SPI failed: {e3}")

    # ── Method 3: SDMMC mode (1-bit) ──
    if not mounted:
        print("Method 3: SDMMC 1-bit mode...")
        try:
            from machine import SDCard
            sd = SDCard(slot=1, width=1)
            os.mount(sd, "/sd")
            print("  Mounted via SDMMC slot=1!")
            mounted = True
        except Exception as e4:
            print(f"  SDMMC failed: {e4}")

    # ── Results ──
    if mounted:
        print("\nSD card mounted at /sd")
        _print_sd_info()
        os.umount("/sd")
        print("SD card test complete.")
    else:
        print("\nAll methods failed. Checklist:")
        print("  1. Is a micro SD card inserted? (FAT32 formatted)")
        print("  2. Try uploading sdcard.py for software SPI support")
        print("  3. Card may need to be formatted as FAT32")


def run_all():
    """Run all hardware tests in sequence."""
    print("=" * 50)
    print("  ESP32-S3 Watch Hardware Test Suite")
    print("=" * 50)
    print(f"Free memory: {gc.mem_free()} bytes")

    test_i2c()
    test_pmic()
    test_rtc()
    test_sdcard()
    test_display()
    test_wifi()
    print("\nTouch test (interactive):")
    test_touch()

    print("\n" + "=" * 50)
    print("  All tests complete!")
    print("=" * 50)
