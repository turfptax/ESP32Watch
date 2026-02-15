"""
boot.py - ESP32-S3 Watch system initialization.
Runs before main.py on every boot/reset.
Kept minimal so the watch boots reliably on battery.
"""
import gc
import esp

# Suppress ESP-IDF debug output on UART
esp.osdebug(None)

# Enable and run garbage collection
gc.enable()
gc.collect()
