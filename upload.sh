#!/bin/bash
# Upload all watch files to ESP32-S3 via mpremote
# Usage: Run this from the ESP32Watch folder
# On Linux, set PORT to your device (e.g. /dev/ttyACM0 or /dev/ttyUSB0)

PORT="/dev/ttyACM0"

echo "=== Creating directories ==="
mpremote connect $PORT mkdir :drivers
mpremote connect $PORT mkdir :apps

echo "=== Uploading core files ==="
mpremote connect $PORT cp board_config.py :board_config.py
mpremote connect $PORT cp boot.py :boot.py
mpremote connect $PORT cp main.py :main.py
mpremote connect $PORT cp watch_ui.py :watch_ui.py
mpremote connect $PORT cp power_manager.py :power_manager.py
mpremote connect $PORT cp logger.py :logger.py
mpremote connect $PORT cp wifi_manager.py :wifi_manager.py
mpremote connect $PORT cp sdcard.py :sdcard.py
mpremote connect $PORT cp test_hardware.py :test_hardware.py
mpremote connect $PORT cp test_display_bringup.py :test_display_bringup.py
mpremote connect $PORT cp test_touch.py :test_touch.py

echo "=== Uploading drivers ==="
mpremote connect $PORT cp drivers/__init__.py :drivers/__init__.py
mpremote connect $PORT cp drivers/co5300.py :drivers/co5300.py
mpremote connect $PORT cp drivers/ft3168.py :drivers/ft3168.py
mpremote connect $PORT cp drivers/axp2101.py :drivers/axp2101.py
mpremote connect $PORT cp drivers/pcf85063.py :drivers/pcf85063.py
mpremote connect $PORT cp drivers/qmi8658.py :drivers/qmi8658.py

echo "=== Uploading apps ==="
mpremote connect $PORT cp apps/__init__.py :apps/__init__.py
mpremote connect $PORT cp apps/weather_watch.py :apps/weather_watch.py

echo "=== Verifying upload ==="
mpremote connect $PORT ls :
mpremote connect $PORT ls :drivers
mpremote connect $PORT ls :apps

echo "=== Done! Reset the board to start. ==="
