# ESP32-S3 Weather Watch — MicroPython

A MicroPython project for the **Waveshare ESP32-S3-Touch-AMOLED-2.06** watch development board. Displays the current time and weather on the 2.06" AMOLED screen with touch navigation.

## What's Included

```
ESP32Watch/
├── board_config.py         # GPIO pin mappings & color constants
├── boot.py                 # System startup config
├── main.py                 # Entry point (edit WiFi/location here)
├── wifi_manager.py         # WiFi connection & NTP time sync
├── test_hardware.py        # Hardware validation test suite
├── drivers/
│   ├── co5300.py           # AMOLED display driver (CO5300 via SPI)
│   ├── ft3168.py           # Capacitive touch driver (FT3168 via I2C)
│   ├── axp2101.py          # Power management driver (AXP2101 via I2C)
│   └── pcf85063.py         # Real-time clock driver (PCF85063 via I2C)
└── apps/
    └── weather_watch.py    # Weather + clock watch face app
```

## Hardware Overview

| Component | Chip | Interface | I2C Addr |
|-----------|------|-----------|----------|
| Display | CO5300 (410x502 AMOLED) | SPI (QSPI capable) | — |
| Touch | FT3168 | I2C | 0x38 |
| IMU | QMI8658 (6-axis) | I2C | 0x6B |
| RTC | PCF85063A | I2C | 0x51 |
| Audio | ES8311 + ES7210 | I2C + I2S | 0x10 |
| PMIC | AXP2101 | I2C | 0x34 |

## Getting Started

### Step 1: Install esptool

```bash
pip install esptool
```

### Step 2: Download MicroPython Firmware

Download the **ESP32-S3** firmware with **PSRAM (OctalSPI)** support from:

https://micropython.org/download/ESP32_GENERIC_S3/

Choose the version labeled: `ESP32_GENERIC_S3-SPIRAM_OCT-20xxxxxx-vX.XX.X.bin`

The board has 32MB flash and 8MB PSRAM (Octal SPI), so the SPIRAM_OCT variant is correct.

### Step 3: Flash MicroPython

1. Connect the watch to your computer via USB-C
2. Hold the **BOOT button** (GPIO0) while pressing **reset** (or while plugging in) to enter download mode
3. Flash the firmware:

```bash
# Erase existing firmware first
esptool.py --chip esp32s3 --port /dev/ttyACM0 erase_flash

# Flash MicroPython (adjust the .bin filename to match what you downloaded)
esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash -z 0x0 \
    ESP32_GENERIC_S3-SPIRAM_OCT-20xxxxxx-vX.XX.X.bin
```

**Windows users**: Replace `/dev/ttyACM0` with your COM port (e.g., `COM3`). Check Device Manager.

**macOS users**: The port will be something like `/dev/cu.usbmodem14101`.

### Step 4: Upload Project Files

Use **mpremote** (recommended) or Thonny IDE:

```bash
pip install mpremote

# Upload all project files
mpremote connect /dev/ttyACM0 cp -r . :
```

Or file by file:
```bash
mpremote connect /dev/ttyACM0 mkdir :drivers
mpremote connect /dev/ttyACM0 mkdir :apps
mpremote connect /dev/ttyACM0 cp board_config.py :board_config.py
mpremote connect /dev/ttyACM0 cp boot.py :boot.py
mpremote connect /dev/ttyACM0 cp main.py :main.py
mpremote connect /dev/ttyACM0 cp wifi_manager.py :wifi_manager.py
mpremote connect /dev/ttyACM0 cp test_hardware.py :test_hardware.py
mpremote connect /dev/ttyACM0 cp drivers/__init__.py :drivers/__init__.py
mpremote connect /dev/ttyACM0 cp drivers/co5300.py :drivers/co5300.py
mpremote connect /dev/ttyACM0 cp drivers/ft3168.py :drivers/ft3168.py
mpremote connect /dev/ttyACM0 cp drivers/axp2101.py :drivers/axp2101.py
mpremote connect /dev/ttyACM0 cp drivers/pcf85063.py :drivers/pcf85063.py
mpremote connect /dev/ttyACM0 cp apps/__init__.py :apps/__init__.py
mpremote connect /dev/ttyACM0 cp apps/weather_watch.py :apps/weather_watch.py
```

### Step 5: Configure Your Settings

Before running, edit `main.py` to set your WiFi credentials and location:

```python
WIFI_SSID     = "YourWiFiName"
WIFI_PASSWORD = "YourWiFiPassword"
LATITUDE      = 40.7128    # Your latitude
LONGITUDE     = -74.0060   # Your longitude
UTC_OFFSET    = -5          # Your timezone offset from UTC
TEMP_UNIT     = "fahrenheit"  # or "celsius"
```

### Step 6: Test the Hardware

Before running the full app, validate each component works:

```bash
mpremote connect /dev/ttyACM0 repl
```

Then in the REPL:
```python
import test_hardware
test_hardware.test_i2c()      # Should find 5 I2C devices
test_hardware.test_display()  # Should show colored bars
test_hardware.test_pmic()     # Should show battery info
test_hardware.test_rtc()      # Should show/set time
test_hardware.test_touch()    # Touch the screen for 10 seconds
test_hardware.test_wifi()     # Scans for nearby networks
```

### Step 7: Run the Watch App

Reset the board (press reset button or power cycle). The watch app in `main.py` will start automatically.

## Important Notes

### Display Performance

This project uses **standard SPI mode** (single data line) instead of QSPI (4 data lines) because MicroPython's `machine.SPI` doesn't support QSPI natively. This means:

- Full-screen refresh is slower (~4x) than QSPI
- The 410x502 framebuffer uses ~401 KB of the 8MB PSRAM
- For smooth animations, use `show_region()` to update only changed areas

For maximum performance, you would need to build a custom MicroPython firmware with a C module that uses ESP-IDF's `esp_lcd` QSPI driver (similar to the [RM67162_Micropython_QSPI](https://github.com/nspsck/RM67162_Micropython_QSPI) project).

### Troubleshooting

- **Display stays black**: The SPI mode switch (QSPI → standard SPI) is the most likely issue. The CO5300 defaults to QSPI mode. If the mode switch command doesn't work reliably, you may need a custom firmware with native QSPI support.
- **I2C devices not found**: Check that the board is powered properly (USB-C connected). The AXP2101 PMIC must be active for peripherals to have power.
- **Touch not responding**: Ensure the touch reset pin (GPIO9) is toggled during init. Check that GPIO38 (interrupt) is properly configured.
- **WiFi won't connect**: Verify your SSID and password. The ESP32-S3 only supports 2.4 GHz WiFi, not 5 GHz.

## Resources

- [Waveshare Wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-2.06) — Official documentation
- [Waveshare GitHub](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.06) — Arduino/ESP-IDF examples
- [CO5300 Datasheet](https://files.waveshare.com/wiki/common/Co5300_Datasheet.pdf)
- [MicroPython ESP32-S3 Downloads](https://micropython.org/download/ESP32_GENERIC_S3/)
- [Open-Meteo Weather API](https://open-meteo.com/) — Free weather data (no API key needed)
