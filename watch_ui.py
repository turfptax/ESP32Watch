"""
Watch UI for Waveshare ESP32-S3-Touch-AMOLED-2.06
Main watch interface with touch navigation between screens.

Usage (from REPL):
    from watch_ui import WatchUI
    ui = WatchUI()
    ui.run()        # Main loop (Ctrl+C to stop)
"""

import time
import gc
from machine import I2C, Pin
import board_config as BOARD
from drivers.co5300 import CO5300
from drivers.pcf85063 import PCF85063
from drivers.axp2101 import AXP2101


# ─── Color palette ─────────────────────────────────────────────
BG           = BOARD.COLOR_BLACK
TEXT_PRIMARY  = BOARD.COLOR_WHITE
TEXT_DIM      = BOARD.rgb565(120, 120, 120)
ACCENT        = BOARD.rgb565(0, 180, 255)      # Cyan-blue
ACCENT2       = BOARD.rgb565(0, 220, 120)       # Green
BAT_LOW       = BOARD.rgb565(255, 60, 60)
BAT_MID       = BOARD.rgb565(255, 180, 0)
BAT_OK        = BOARD.rgb565(0, 220, 120)
SEPARATOR     = BOARD.rgb565(40, 40, 50)
HIGHLIGHT     = BOARD.rgb565(0, 120, 200)       # Button press highlight

# Day and month names
DAYS   = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Screen IDs
SCREEN_CLOCK = 0
SCREEN_INFO  = 1
SCREEN_SETUP = 2
SCREEN_NAMES = ("Clock", "Info", "Setup")


class WatchUI:
    """Watch face with touch navigation between screens."""

    def __init__(self):
        # Initialize display
        print("Initializing display...")
        self.display = CO5300()
        self.display.init()

        # Initialize I2C bus (shared)
        print("Initializing I2C bus...")
        self.i2c = I2C(0, sda=Pin(BOARD.I2C_SDA),
                       scl=Pin(BOARD.I2C_SCL),
                       freq=BOARD.I2C_FREQ)

        # Initialize peripherals
        print("Initializing RTC...")
        self.rtc = PCF85063(self.i2c)

        print("Initializing PMIC...")
        self.pmic = AXP2101(self.i2c)
        self.pmic.init()

        # TCA9554 — enable all outputs (touch power, etc.)
        self._tca9554_init()

        # Touch — set up interrupt pin and try init
        print("Initializing touch...")
        self._touch_addr = BOARD.TOUCH_ADDR
        self._touch_int = Pin(BOARD.TP_INT, Pin.IN)
        self._touch_rst = Pin(BOARD.TP_RESET, Pin.OUT, value=1)
        self._touch_ok = self._touch_init()

        # IMU — for motion detection (graceful fallback if absent)
        self._init_imu()

        # Power manager — handles display sleep/wake and light sleep
        from power_manager import PowerManager
        self._power = PowerManager(
            display=self.display,
            imu=self._imu,
            touch_int_pin=self._touch_int,
        )

        # Screen dimensions
        self.W = BOARD.LCD_WIDTH   # 410
        self.H = BOARD.LCD_HEIGHT  # 502

        # UI state
        self.screen = SCREEN_CLOCK
        self._last_minute = -1
        self._last_bat = -1
        self._last_touch_time = 0    # Debounce
        self._needs_redraw = True

        # Button hit zones: (x, y, w, h) for the 3 bottom buttons
        btn_w = 100
        btn_h = 50
        btn_y = 420
        gap = (self.W - 3 * btn_w) // 4
        self._buttons = []
        for i in range(3):
            bx = gap + i * (btn_w + gap)
            self._buttons.append((bx, btn_y, btn_w, btn_h))

        print("Watch UI ready!")

    # ─── TCA9554 GPIO expander ────────────────────────────────

    def _tca9554_init(self):
        """Enable all TCA9554 outputs HIGH."""
        addr = BOARD.EXPANDER_ADDR
        try:
            self.i2c.writeto(addr, bytes([0x03, 0x00]))  # All outputs
            self.i2c.writeto(addr, bytes([0x01, 0xFF]))   # All HIGH
            print("TCA9554: outputs enabled")
        except Exception as e:
            print(f"TCA9554 error: {e}")

    # ─── IMU (QMI8658) ─────────────────────────────────────────

    def _init_imu(self):
        """Initialize IMU for motion detection. Falls back to None."""
        self._imu = None
        try:
            from drivers.qmi8658 import QMI8658
            self._imu = QMI8658(self.i2c)
            self._imu.init()
        except Exception as e:
            print(f"IMU not available: {e}")

    # ─── Touch controller ─────────────────────────────────────

    def _touch_init(self):
        """Initialize touch controller with reset."""
        # Hardware reset
        self._touch_rst(0)
        time.sleep_ms(20)
        self._touch_rst(1)
        time.sleep_ms(300)

        # Check if device responds
        try:
            self.i2c.writeto(self._touch_addr, bytes([0x00]))
            self.i2c.readfrom(self._touch_addr, 1)
            print(f"Touch controller at 0x{self._touch_addr:02X}: OK")
            return True
        except:
            print("Touch controller: not responding")
            return False

    def _read_touch(self):
        """Read touch point. Returns (x, y) or None."""
        if not self._touch_ok:
            return None

        # Check interrupt pin (active LOW = touch active)
        if self._touch_int():
            return None

        try:
            self.i2c.writeto(self._touch_addr, bytes([0x02]))
            data = self.i2c.readfrom(self._touch_addr, 5)
            num = data[0] & 0x0F
            if num > 0 and num <= 2:
                x = ((data[1] & 0x0F) << 8) | data[2]
                y = ((data[3] & 0x0F) << 8) | data[4]
                return (x, y)
        except:
            pass
        return None

    def _handle_touch(self):
        """Process touch input with debouncing."""
        point = self._read_touch()
        if point is None:
            return

        # Reset display timeout on any touch
        self._power.activity()

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_touch_time) < 300:
            return  # Debounce — ignore touches within 300ms
        self._last_touch_time = now

        x, y = point

        # Check which button was tapped
        for i, (bx, by, bw, bh) in enumerate(self._buttons):
            if bx <= x <= bx + bw and by <= y <= by + bh:
                if i != self.screen:
                    self.screen = i
                    self._needs_redraw = True
                    print(f"Touch: switched to {SCREEN_NAMES[i]} screen")
                return

        # Tap anywhere in upper area = toggle between clock/info
        if y < 400:
            self.screen = SCREEN_INFO if self.screen == SCREEN_CLOCK else SCREEN_CLOCK
            self._needs_redraw = True
            print(f"Touch: toggled to {SCREEN_NAMES[self.screen]} screen")

    # ─── Drawing helpers ──────────────────────────────────────

    def _center_text(self, text, y, color, scale=1):
        """Draw text horizontally centered."""
        char_w = 8 * scale
        text_w = len(text) * char_w
        x = (self.W - text_w) // 2
        self.display.text(text, x, y, color, scale)

    def _draw_battery(self, x, y, percent):
        """Draw battery icon with fill level."""
        bw, bh = 40, 18
        self.display.rect(x, y, bw, bh, TEXT_DIM)
        self.display.rect(x + bw, y + 5, 4, 8, TEXT_DIM, fill=True)

        fill_max = bw - 4
        fill_w = max(0, int(fill_max * percent / 100))

        if percent <= 15:
            fc = BAT_LOW
        elif percent <= 40:
            fc = BAT_MID
        else:
            fc = BAT_OK

        if fill_w > 0:
            self.display.rect(x + 2, y + 2, fill_w, bh - 4, fc, fill=True)

        self.display.text(f"{percent}%", x + bw + 10, y + 5, TEXT_DIM, 1)

    def _draw_divider(self, y):
        """Draw a subtle horizontal divider line."""
        margin = 40
        self.display.hline(margin, y, self.W - margin * 2, SEPARATOR)

    def _draw_rounded_rect(self, x, y, w, h, color, fill=False):
        """Draw a rectangle with rounded corners."""
        if fill:
            self.display.rect(x + 2, y, w - 4, h, color, fill=True)
            self.display.rect(x, y + 2, w, h - 4, color, fill=True)
            self.display.rect(x + 1, y + 1, w - 2, h - 2, color, fill=True)
        else:
            self.display.hline(x + 2, y, w - 4, color)
            self.display.hline(x + 2, y + h - 1, w - 4, color)
            self.display.vline(x, y + 2, h - 4, color)
            self.display.vline(x + w - 1, y + 2, h - 4, color)
            self.display.pixel(x + 1, y + 1, color)
            self.display.pixel(x + w - 2, y + 1, color)
            self.display.pixel(x + 1, y + h - 2, color)
            self.display.pixel(x + w - 2, y + h - 2, color)

    def _draw_status_bar(self):
        """Draw the top status bar (shared across all screens)."""
        bat = self.pmic.battery_percent
        usb = self.pmic.is_vbus_present
        self._draw_battery(10, 10, bat)
        if usb:
            self.display.text("USB", self.W - 40, 14, ACCENT, 1)
        self._draw_divider(38)
        self._last_bat = bat

    def _draw_nav_buttons(self):
        """Draw bottom navigation buttons with active highlight."""
        labels = list(SCREEN_NAMES)
        for i, (bx, by, bw, bh) in enumerate(self._buttons):
            if i == self.screen:
                # Active button — filled
                self._draw_rounded_rect(bx, by, bw, bh, ACCENT, fill=True)
                lx = bx + (bw - len(labels[i]) * 8 * 2) // 2
                ly = by + (bh - 16) // 2
                self.display.text(labels[i], lx, ly, BG, 2)
            else:
                # Inactive button — outline only
                self._draw_rounded_rect(bx, by, bw, bh, ACCENT)
                lx = bx + (bw - len(labels[i]) * 8 * 2) // 2
                ly = by + (bh - 16) // 2
                self.display.text(labels[i], lx, ly, ACCENT, 2)

    # ─── Screen: Clock ────────────────────────────────────────

    def _draw_clock_screen(self):
        """Main clock face."""
        d = self.display
        dt = self.rtc.datetime()
        year, month, day, weekday, hour, minute, second = dt

        # Large time
        time_str = f"{hour:02d}:{minute:02d}"
        self._center_text(time_str, 70, TEXT_PRIMARY, 7)

        # Seconds
        sec_str = f":{second:02d}"
        sec_x = (self.W + len(time_str) * 56) // 2 - 60
        d.text(sec_str, sec_x, 130, TEXT_DIM, 3)

        self._draw_divider(170)

        # Date
        day_name = DAYS[weekday] if weekday < 7 else "???"
        month_name = MONTHS[month] if 1 <= month <= 12 else "???"
        date_str = f"{day_name}, {month_name} {day}"
        self._center_text(date_str, 190, ACCENT, 2)
        self._center_text(str(year), 220, TEXT_DIM, 2)

        self._draw_divider(255)

        # Info card
        cx, cy = 30, 275
        cw, ch = self.W - 60, 110
        self._draw_rounded_rect(cx, cy, cw, ch, SEPARATOR)
        d.text("System Status", cx + 15, cy + 12, ACCENT, 2)
        bat = self.pmic.battery_percent
        d.text(f"Battery:  {bat}%", cx + 15, cy + 42, TEXT_PRIMARY, 2)
        vbat = self.pmic.battery_voltage
        d.text(f"VBAT:     {vbat}mV", cx + 15, cy + 68, TEXT_DIM, 2)

        self._last_minute = minute

    # ─── Screen: Info ─────────────────────────────────────────

    def _draw_info_screen(self):
        """System information screen."""
        d = self.display
        y = 55
        spacing = 32

        self._center_text("System Info", y, ACCENT, 3)
        y += 45

        self._draw_divider(y)
        y += 20

        # Battery details
        bat = self.pmic.battery_percent
        vbat = self.pmic.battery_voltage
        usb = self.pmic.is_vbus_present
        charging = self.pmic.is_charging

        items = [
            ("Battery", f"{bat}%"),
            ("Voltage", f"{vbat} mV"),
            ("USB", "Connected" if usb else "No"),
            ("Charging", "Yes" if charging else "No"),
        ]

        for label, value in items:
            d.text(label, 40, y, TEXT_DIM, 2)
            d.text(value, 230, y, TEXT_PRIMARY, 2)
            y += spacing

        self._draw_divider(y + 5)
        y += 20

        # Memory
        gc.collect()
        free = gc.mem_free()
        if free > 1024 * 1024:
            mem_str = f"{free // (1024*1024)}.{(free % (1024*1024)) // 102400} MB"
        else:
            mem_str = f"{free // 1024} KB"

        d.text("Free RAM", 40, y, TEXT_DIM, 2)
        d.text(mem_str, 230, y, TEXT_PRIMARY, 2)
        y += spacing

        # Touch status
        d.text("Touch", 40, y, TEXT_DIM, 2)
        d.text("OK" if self._touch_ok else "N/A", 230, y,
               ACCENT2 if self._touch_ok else BAT_LOW, 2)
        y += spacing

        # IMU status
        d.text("IMU", 40, y, TEXT_DIM, 2)
        d.text("OK" if self._imu else "N/A", 230, y,
               ACCENT2 if self._imu else BAT_LOW, 2)
        y += spacing

        # RTC time
        dt = self.rtc.datetime()
        t_str = f"{dt[3]:02d}:{dt[4]:02d}:{dt[5]:02d}"
        d.text("RTC", 40, y, TEXT_DIM, 2)
        d.text(t_str, 230, y, TEXT_PRIMARY, 2)
        y += spacing

        # I2C devices
        devs = self.i2c.scan()
        d.text("I2C devs", 40, y, TEXT_DIM, 2)
        d.text(str(len(devs)), 230, y, TEXT_PRIMARY, 2)

    # ─── Screen: Setup ────────────────────────────────────────

    def _draw_setup_screen(self):
        """Settings/setup screen."""
        d = self.display
        y = 55

        self._center_text("Settings", y, ACCENT, 3)
        y += 45
        self._draw_divider(y)
        y += 25

        # Display some configurable items
        options = [
            ("Brightness", "100%"),
            ("Time Zone", "UTC-5"),
            ("Temp Unit", "F"),
            ("WiFi", "Off"),
            ("Time Format", "24h"),
        ]

        for i, (label, value) in enumerate(options):
            row_y = y + i * 55

            # Option row background
            self._draw_rounded_rect(30, row_y, self.W - 60, 45, SEPARATOR)

            # Label
            d.text(label, 50, row_y + 14, TEXT_PRIMARY, 2)

            # Value (right-aligned)
            val_w = len(value) * 16
            d.text(value, self.W - 50 - val_w, row_y + 14, ACCENT, 2)

    # ─── Main draw/update ─────────────────────────────────────

    def draw(self):
        """Full screen draw for the current screen."""
        d = self.display
        d.fill(BG)

        self._draw_status_bar()

        if self.screen == SCREEN_CLOCK:
            self._draw_clock_screen()
        elif self.screen == SCREEN_INFO:
            self._draw_info_screen()
        elif self.screen == SCREEN_SETUP:
            self._draw_setup_screen()

        self._draw_nav_buttons()
        d.show()
        self._needs_redraw = False

    def _update_seconds(self):
        """Quick seconds-only update for clock screen."""
        d = self.display
        dt = self.rtc.datetime()
        minute = dt[4]
        second = dt[5]
        hour = dt[3]

        # Full redraw if minute changed
        if minute != self._last_minute:
            self._needs_redraw = True
            return

        # Just update seconds area
        time_str = f"{hour:02d}:{minute:02d}"
        sec_x = (self.W + len(time_str) * 56) // 2 - 60
        d.rect(sec_x, 130, 80, 30, BG, fill=True)
        d.text(f":{second:02d}", sec_x, 130, TEXT_DIM, 3)
        d.show_region(sec_x, 130, 80, 30)

    def run(self):
        """Main UI loop with sleep/wake power management.
        Press Ctrl+C to stop when connected via serial.
        """
        print("Starting watch UI...")
        self.draw()

        try:
            while True:
                if self._power.is_display_on:
                    # ── ACTIVE PHASE: display on, handling UI ──
                    self._handle_touch()

                    if self._needs_redraw:
                        self.draw()
                    elif self.screen == SCREEN_CLOCK:
                        self._update_seconds()

                    # Check if we should sleep the display
                    if self._power.check_timeout():
                        gc.collect()

                    time.sleep_ms(50)  # ~20Hz poll rate

                else:
                    # ── SLEEP PHASE: display off, light sleep ──
                    cause = self._power.enter_light_sleep()

                    if cause in ('touch', 'motion'):
                        # Woke up — redraw the screen
                        self._needs_redraw = True
                    # 'timer' with no motion: loop back to sleep

        except KeyboardInterrupt:
            print("\nWatch UI stopped.")
