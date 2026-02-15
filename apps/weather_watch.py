"""
Weather Watch Face - WiFi-connected weather + clock display
Fetches weather data from Open-Meteo (free, no API key needed)
and displays it on the AMOLED with touch interaction.

Usage:
    from apps.weather_watch import WeatherWatch
    app = WeatherWatch(ssid="YourWiFi", password="YourPassword")
    app.run()
"""

import time
import gc
import json
import urequests
from machine import I2C, Pin

import board_config as BOARD
from drivers.co5300 import CO5300
from drivers.ft3168 import FT3168
from drivers.axp2101 import AXP2101
from drivers.pcf85063 import PCF85063
from wifi_manager import WiFiManager


# ─── Weather Code Descriptions ──────────────────────────────────
WMO_CODES = {
    0: "Clear",
    1: "Mostly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    80: "Light Showers",
    81: "Showers",
    82: "Heavy Showers",
    95: "Thunderstorm",
}


class WeatherWatch:
    """WiFi-connected weather watch face."""

    def __init__(self, ssid, password,
                 latitude=40.71, longitude=-74.01,  # NYC default
                 utc_offset=-5, temp_unit="fahrenheit"):
        """
        Args:
            ssid:       WiFi network name
            password:   WiFi password
            latitude:   Your latitude (for weather location)
            longitude:  Your longitude
            utc_offset: Hours from UTC (e.g. -5 for EST, +1 for CET)
            temp_unit:  "fahrenheit" or "celsius"
        """
        self.ssid = ssid
        self.password = password
        self.latitude = latitude
        self.longitude = longitude
        self.utc_offset = utc_offset
        self.temp_unit = temp_unit

        # Weather data cache
        self.weather = {
            "temp": "--",
            "feels_like": "--",
            "condition": "Loading...",
            "code": 0,
            "humidity": "--",
            "wind": "--",
            "high": "--",
            "low": "--",
        }
        self._last_weather_fetch = 0
        self._weather_interval = 600  # Refresh every 10 minutes

        # UI state
        self._screen = "clock"  # "clock" or "detail"
        self._brightness = 200

    def run(self):
        """Main application entry point."""
        print("Initializing Weather Watch...")

        # Initialize I2C bus (shared by all peripherals)
        i2c = I2C(0, sda=Pin(BOARD.I2C_SDA), scl=Pin(BOARD.I2C_SCL),
                   freq=BOARD.I2C_FREQ)

        # Scan I2C bus
        devices = i2c.scan()
        print(f"I2C devices found: {[hex(d) for d in devices]}")

        # Initialize display
        print("Initializing display...")
        self.display = CO5300()
        self.display.init()
        self.display.brightness(self._brightness)

        # Show boot screen
        self._draw_boot_screen("Initializing...")
        self.display.show()

        # Initialize touch
        print("Initializing touch...")
        self.touch = FT3168(i2c)
        self.touch.init()

        # Initialize PMIC
        print("Initializing power management...")
        self.pmic = AXP2101(i2c)
        self.pmic.init()

        # Initialize RTC
        print("Initializing RTC...")
        self.rtc = PCF85063(i2c)

        # Connect WiFi
        self._draw_boot_screen("Connecting WiFi...")
        self.display.show()

        self.wifi = WiFiManager(self.ssid, self.password)
        if self.wifi.connect(timeout=15):
            self._draw_boot_screen("Syncing time...")
            self.display.show()
            self.wifi.sync_ntp(utc_offset=self.utc_offset)

            # Set RTC from NTP time
            t = time.localtime(time.time() + self.utc_offset * 3600)
            self.rtc.datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5]))

            # Fetch initial weather
            self._draw_boot_screen("Fetching weather...")
            self.display.show()
            self._fetch_weather()
        else:
            self._draw_boot_screen("WiFi failed - offline mode")
            self.display.show()
            time.sleep(2)

        # Main loop
        print("Starting main loop...")
        self._main_loop()

    def _main_loop(self):
        """Main application loop."""
        last_draw = 0

        while True:
            now = time.time()

            # Handle touch input
            if self.touch.touched:
                points = self.touch.read()
                if points:
                    self._handle_touch(points[0])
                    time.sleep_ms(200)  # Debounce

            # Update display every second
            if now - last_draw >= 1:
                last_draw = now
                if self._screen == "clock":
                    self._draw_clock_face()
                elif self._screen == "detail":
                    self._draw_detail_screen()
                self.display.show()

            # Refresh weather periodically
            if now - self._last_weather_fetch >= self._weather_interval:
                if self.wifi.is_connected:
                    self._fetch_weather()
                else:
                    # Try reconnecting
                    self.wifi.connect(timeout=5)

            # Brief sleep to avoid hogging CPU
            time.sleep_ms(50)
            gc.collect()

    # ─── Touch handling ──────────────────────────────────────────

    def _handle_touch(self, point):
        """Handle a touch event."""
        x, y, event = point

        if self._screen == "clock":
            # Tap anywhere to switch to detail screen
            self._screen = "detail"
        elif self._screen == "detail":
            # Tap top half to go back to clock
            if y < BOARD.LCD_HEIGHT // 2:
                self._screen = "clock"
            # Tap bottom half to force weather refresh
            else:
                if self.wifi.is_connected:
                    self._fetch_weather()

    # ─── Weather fetching ────────────────────────────────────────

    def _fetch_weather(self):
        """Fetch current weather from Open-Meteo API (free, no key)."""
        try:
            unit = self.temp_unit
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={self.latitude}&longitude={self.longitude}"
                f"&current=temperature_2m,relative_humidity_2m,"
                f"apparent_temperature,weather_code,wind_speed_10m"
                f"&daily=temperature_2m_max,temperature_2m_min"
                f"&temperature_unit={unit}"
                f"&wind_speed_unit=mph"
                f"&forecast_days=1"
            )

            resp = urequests.get(url)
            data = resp.json()
            resp.close()

            current = data.get("current", {})
            daily = data.get("daily", {})

            self.weather["temp"] = f"{current.get('temperature_2m', '--')}"
            self.weather["feels_like"] = f"{current.get('apparent_temperature', '--')}"
            self.weather["code"] = current.get("weather_code", 0)
            self.weather["condition"] = WMO_CODES.get(
                self.weather["code"], "Unknown"
            )
            self.weather["humidity"] = f"{current.get('relative_humidity_2m', '--')}%"
            self.weather["wind"] = f"{current.get('wind_speed_10m', '--')} mph"

            if daily.get("temperature_2m_max"):
                self.weather["high"] = f"{daily['temperature_2m_max'][0]}"
            if daily.get("temperature_2m_min"):
                self.weather["low"] = f"{daily['temperature_2m_min'][0]}"

            self._last_weather_fetch = time.time()
            print(f"Weather: {self.weather['temp']}° {self.weather['condition']}")

        except Exception as e:
            print(f"Weather fetch error: {e}")

    # ─── Drawing routines ────────────────────────────────────────

    def _draw_boot_screen(self, message):
        """Draw a simple boot/loading screen."""
        self.display.fill(BOARD.COLOR_BLACK)
        self.display.text("ESP32 Watch", 130, 200, BOARD.COLOR_CYAN, scale=2)
        self.display.text(message, 100, 260, BOARD.COLOR_GRAY)

    def _draw_clock_face(self):
        """Draw the main clock + weather watch face."""
        W = self.display.width
        H = self.display.height

        self.display.fill(BOARD.COLOR_BLACK)

        # ── Time (big, centered) ──
        dt = self.rtc.datetime()
        year, month, day, weekday, hour, minute, second = dt

        # 12-hour format
        ampm = "AM" if hour < 12 else "PM"
        h12 = hour % 12
        if h12 == 0:
            h12 = 12

        time_str = f"{h12}:{minute:02d}"
        # Large time display (scale 4 = 32px height)
        tx = (W - len(time_str) * 8 * 4) // 2
        self.display.text(time_str, tx, 120, BOARD.COLOR_WHITE, scale=4)

        # Seconds + AM/PM
        sec_str = f":{second:02d} {ampm}"
        self.display.text(sec_str, tx + len(time_str) * 8 * 4 + 4, 135,
                          BOARD.COLOR_GRAY, scale=2)

        # ── Date ──
        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        day_name = days[weekday] if weekday < 7 else "?"
        month_name = months[month - 1] if 1 <= month <= 12 else "?"
        date_str = f"{day_name}, {month_name} {day}"
        dx = (W - len(date_str) * 8 * 2) // 2
        self.display.text(date_str, dx, 180, BOARD.COLOR_CYAN, scale=2)

        # ── Separator line ──
        self.display.hline(30, 220, W - 60, BOARD.COLOR_GRAY)

        # ── Weather info ──
        unit_sym = "F" if self.temp_unit == "fahrenheit" else "C"

        # Current temp (big)
        temp_str = f"{self.weather['temp']}{unit_sym}"
        self.display.text(temp_str, 40, 250, BOARD.COLOR_ORANGE, scale=3)

        # Condition
        self.display.text(self.weather["condition"], 40, 300, BOARD.COLOR_WHITE, scale=2)

        # High/Low
        hl_str = f"H:{self.weather['high']}  L:{self.weather['low']}"
        self.display.text(hl_str, 40, 340, BOARD.COLOR_GRAY)

        # Humidity + Wind
        self.display.text(f"Humidity: {self.weather['humidity']}", 40, 370, BOARD.COLOR_GRAY)
        self.display.text(f"Wind: {self.weather['wind']}", 40, 390, BOARD.COLOR_GRAY)

        # ── Battery indicator (top-right) ──
        batt = self.pmic.battery_percent
        batt_color = BOARD.COLOR_GREEN
        if batt < 20:
            batt_color = BOARD.COLOR_RED
        elif batt < 50:
            batt_color = BOARD.COLOR_YELLOW
        self.display.text(f"{batt}%", W - 50, 10, batt_color)

        # WiFi indicator (top-left)
        if self.wifi.is_connected:
            self.display.text("WiFi", 10, 10, BOARD.COLOR_GREEN)
        else:
            self.display.text("WiFi", 10, 10, BOARD.COLOR_RED)

        # ── Hint ──
        self.display.text("Tap for details", (W - 15 * 8) // 2, H - 30,
                          BOARD.COLOR_GRAY)

    def _draw_detail_screen(self):
        """Draw the detail/info screen."""
        W = self.display.width
        H = self.display.height

        self.display.fill(BOARD.COLOR_BLACK)

        self.display.text("System Info", 20, 20, BOARD.COLOR_CYAN, scale=2)
        self.display.hline(20, 50, W - 40, BOARD.COLOR_GRAY)

        y = 70
        gap = 30

        # Battery details
        self.display.text(f"Battery:   {self.pmic.battery_percent}%", 20, y, BOARD.COLOR_WHITE)
        y += gap
        self.display.text(f"Charging:  {'Yes' if self.pmic.is_charging else 'No'}",
                          20, y, BOARD.COLOR_WHITE)
        y += gap
        self.display.text(f"USB:       {'Yes' if self.pmic.is_vbus_present else 'No'}",
                          20, y, BOARD.COLOR_WHITE)
        y += gap

        # WiFi details
        self.display.hline(20, y, W - 40, BOARD.COLOR_GRAY)
        y += 15
        self.display.text(f"WiFi:      {self.ssid}", 20, y, BOARD.COLOR_WHITE)
        y += gap
        if self.wifi.is_connected:
            self.display.text(f"IP:        {self.wifi.ip_address}", 20, y, BOARD.COLOR_WHITE)
            y += gap
            rssi = self.wifi.rssi
            if rssi:
                self.display.text(f"Signal:    {rssi} dBm", 20, y, BOARD.COLOR_WHITE)
                y += gap

        # Memory
        self.display.hline(20, y, W - 40, BOARD.COLOR_GRAY)
        y += 15
        free = gc.mem_free()
        self.display.text(f"Free RAM:  {free // 1024} KB", 20, y, BOARD.COLOR_WHITE)
        y += gap

        # Weather location
        self.display.text(f"Lat:       {self.latitude}", 20, y, BOARD.COLOR_WHITE)
        y += gap
        self.display.text(f"Lon:       {self.longitude}", 20, y, BOARD.COLOR_WHITE)
        y += gap

        # Navigation hints
        self.display.text("Tap top: back to clock", 20, H - 60, BOARD.COLOR_GRAY)
        self.display.text("Tap bottom: refresh weather", 20, H - 35, BOARD.COLOR_GRAY)
