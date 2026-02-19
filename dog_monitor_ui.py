"""
Dog Audio Monitor UI for Waveshare ESP32-S3-Touch-AMOLED-2.06
Dedicated dog vocalization detector — replaces the watch UI.

Continuously monitors audio via the ES7210 ADC and dual MEMS microphones.
When a bark, whine, or vocalization exceeds the volume threshold,
records a WAV clip to the SD card for later AI analysis.

Usage (from REPL):
    from dog_monitor_ui import DogMonitorUI
    ui = DogMonitorUI()
    ui.run()
"""

import gc
import os
import time
from machine import I2C, Pin

import board_config as BOARD
from drivers.co5300 import CO5300
from drivers.pcf85063 import PCF85063
from drivers.axp2101 import AXP2101
from audio_recorder import AudioRecorder


# ─── Color palette ─────────────────────────────────────────────
BG             = BOARD.COLOR_BLACK
TEXT_PRIMARY   = BOARD.COLOR_WHITE
TEXT_DIM       = BOARD.rgb565(120, 120, 120)
ACCENT         = BOARD.rgb565(0, 180, 255)       # Cyan
GREEN          = BOARD.rgb565(0, 220, 80)
ORANGE         = BOARD.rgb565(255, 160, 0)
RED            = BOARD.COLOR_RED
BAR_BG         = BOARD.rgb565(30, 30, 40)
SEPARATOR      = BOARD.rgb565(40, 40, 50)
BTN_BG         = BOARD.rgb565(35, 35, 50)
BTN_ACTIVE     = BOARD.rgb565(0, 120, 200)
REC_RED        = BOARD.rgb565(255, 40, 40)

# ─── Layout constants ─────────────────────────────────────────
W = BOARD.LCD_WIDTH    # 410
H = BOARD.LCD_HEIGHT   # 502

# Screen IDs
SCREEN_MAIN     = 0
SCREEN_SETTINGS = 1


class DogMonitorUI:
    """Dog vocalization monitor with sound-triggered WAV recording."""

    def __init__(self, log=None):
        self._log = log

        # ── Display ──
        print("Initializing display...")
        self.display = CO5300()
        self.display.init()

        # ── I2C bus ──
        print("Initializing I2C bus...")
        self.i2c = I2C(0, sda=Pin(BOARD.I2C_SDA),
                       scl=Pin(BOARD.I2C_SCL),
                       freq=BOARD.I2C_FREQ)

        # ── PMIC (battery) ──
        print("Initializing PMIC...")
        self.pmic = AXP2101(self.i2c)
        self.pmic.init()

        # ── TCA9554 ──
        self._tca9554_init()

        # ── Touch ──
        print("Initializing touch...")
        self._touch_addr = BOARD.TOUCH_ADDR
        self._touch_int = Pin(BOARD.TP_INT, Pin.IN)
        self._touch_rst = Pin(BOARD.TP_RESET, Pin.OUT, value=1)
        self._touch_ok = self._touch_init()

        # ── RTC ──
        print("Initializing RTC...")
        self.rtc = PCF85063(self.i2c)
        if self._log:
            self._log._rtc = self.rtc

        # ── Audio recorder ──
        print("Initializing audio recorder...")
        self.recorder = AudioRecorder(self.i2c, rtc=self.rtc, log=self._log)
        self.recorder.init()

        # ── Power manager (display timeout only, no light sleep) ──
        from power_manager import PowerManager
        self._power = PowerManager(
            display=self.display,
            imu=None,               # No IMU needed for audio monitor
            touch_int_pin=self._touch_int,
        )

        # ── UI state ──
        self._screen = SCREEN_MAIN
        self._needs_redraw = True
        self._last_touch_time = 0
        self._last_vu_rms = -1
        self._last_rec_state = None
        self._last_clip_count = -1
        self._rec_blink = False
        self._last_blink_time = 0
        self._sd_free_mb = self._get_sd_free_mb()

        # Settings screen state
        self._settings_items = [
            {'label': 'Threshold', 'get': lambda: self.recorder.trigger_threshold,
             'step': 500, 'min': 500, 'max': 15000,
             'set': lambda v: self.recorder.set_threshold(v)},
            {'label': 'Mic Gain', 'get': lambda: BOARD.AUDIO_MIC_GAIN_DB,
             'step': 6, 'min': 0, 'max': 42,
             'set': lambda v: self._set_gain(v)},
            {'label': 'Max Clip (s)', 'get': lambda: self.recorder._max_clip_sec,
             'step': 10, 'min': 5, 'max': 120,
             'set': lambda v: setattr(self.recorder, '_max_clip_sec', v)},
        ]
        self._mic_gain_db = BOARD.AUDIO_MIC_GAIN_DB

        print("Dog Monitor UI ready!")

    # ─── Hardware init helpers ────────────────────────────────────

    def _tca9554_init(self):
        try:
            self.i2c.writeto(BOARD.EXPANDER_ADDR, bytes([0x03, 0x00]))
            self.i2c.writeto(BOARD.EXPANDER_ADDR, bytes([0x01, 0xFF]))
            print("TCA9554: outputs enabled")
        except Exception as e:
            print(f"TCA9554 error: {e}")

    def _touch_init(self):
        self._touch_rst(0)
        time.sleep_ms(20)
        self._touch_rst(1)
        time.sleep_ms(300)
        try:
            self.i2c.writeto(self._touch_addr, bytes([0x00]))
            self.i2c.readfrom(self._touch_addr, 1)
            print(f"Touch controller at 0x{self._touch_addr:02X}: OK")
            return True
        except:
            print("Touch controller: not responding")
            return False

    def _read_touch(self):
        if not self._touch_ok:
            return None
        if self._touch_int():
            return None
        try:
            self.i2c.writeto(self._touch_addr, bytes([0x02]))
            data = self.i2c.readfrom(self._touch_addr, 5)
            num = data[0] & 0x0F
            if 0 < num <= 2:
                x = ((data[1] & 0x0F) << 8) | data[2]
                y = ((data[3] & 0x0F) << 8) | data[4]
                return (x, y)
        except:
            pass
        return None

    def _set_gain(self, db):
        self._mic_gain_db = db
        self.recorder.set_mic_gain(db)

    # ─── SD card info ─────────────────────────────────────────────

    def _get_sd_free_mb(self):
        try:
            st = os.statvfs("/sd")
            return (st[0] * st[3]) // (1024 * 1024)
        except:
            return -1

    def _get_recent_clips(self, n=6):
        """Get the N most recent clip filenames from /sd/clips/."""
        try:
            files = os.listdir(BOARD.CLIPS_DIR)
            # Filter WAV files and sort descending (newest first by name)
            wavs = sorted([f for f in files if f.endswith('.wav')],
                          reverse=True)
            clips = []
            for fname in wavs[:n]:
                try:
                    size = os.stat(BOARD.CLIPS_DIR + "/" + fname)[6]
                    # Duration approx: (size - 44) / (sample_rate * 2)
                    dur = max(0, (size - 44)) / (BOARD.AUDIO_SAMPLE_RATE * 2)
                    clips.append((fname, dur))
                except:
                    clips.append((fname, 0))
            return clips
        except:
            return []

    # ─── Drawing: Main Screen ─────────────────────────────────────

    def draw(self):
        """Full screen redraw."""
        if self._screen == SCREEN_MAIN:
            self._draw_main()
        elif self._screen == SCREEN_SETTINGS:
            self._draw_settings()
        self._needs_redraw = False

    def _draw_main(self):
        """Draw the main monitoring screen."""
        d = self.display
        d.fill(BG)

        # ── Status bar (y=5..30) ──
        bat = self.pmic.battery_percent
        charging = self.pmic.is_charging
        bat_str = f"{'CHG ' if charging else ''}{bat}%"
        d.text(bat_str, 10, 8, GREEN if bat > 30 else ORANGE, 2)

        if self._sd_free_mb >= 0:
            sd_str = f"SD: {self._sd_free_mb}MB"
            d.text(sd_str, W - len(sd_str) * 12 - 10, 8, TEXT_DIM, 2)
        else:
            d.text("NO SD", W - 70, 8, RED, 2)

        # Separator
        d.hline(0, 35, W, SEPARATOR)

        # ── Title (y=42..65) ──
        title = "DOG AUDIO MONITOR"
        tw = len(title) * 12
        d.text(title, (W - tw) // 2, 45, ACCENT, 2)

        # Separator
        d.hline(0, 72, W, SEPARATOR)

        # ── VU meter area (y=80..135) ──
        self._draw_vu_meter_full()

        # ── Recording status (y=150..200) ──
        self._draw_rec_status_full()

        # Separator
        d.hline(10, 210, W - 20, SEPARATOR)

        # ── Stats (y=218..240) ──
        self._draw_stats()

        # Separator
        d.hline(10, 250, W - 20, SEPARATOR)

        # ── Recent clips (y=258..440) ──
        self._draw_clips_list()

        # Separator
        d.hline(0, 448, W, SEPARATOR)

        # ── Bottom buttons (y=455..495) ──
        self._draw_buttons()

        d.show()

    def _draw_vu_meter_full(self):
        """Draw VU meter bar and labels (y=80..140)."""
        d = self.display
        rms = self.recorder.current_rms
        thresh = self.recorder.trigger_threshold

        # Label
        d.text("Level", 10, 82, TEXT_DIM, 2)

        # Bar background
        bar_x = 10
        bar_y = 105
        bar_w = W - 20
        bar_h = 20
        d.rect(bar_x, bar_y, bar_w, bar_h, BAR_BG, True)

        # Bar fill
        max_rms = thresh * 2
        fill_w = min(bar_w, rms * bar_w // max(max_rms, 1))
        if fill_w > 0:
            color = GREEN
            if rms >= thresh:
                color = RED
            elif rms >= thresh * 2 // 3:
                color = ORANGE
            d.rect(bar_x, bar_y, fill_w, bar_h, color, True)

        # Threshold marker (vertical line)
        marker_x = bar_x + (thresh * bar_w // max(max_rms, 1))
        if bar_x < marker_x < bar_x + bar_w:
            d.vline(marker_x, bar_y, bar_h, TEXT_PRIMARY)

        # Numbers
        d.text(f"{rms}", 10, 130, TEXT_PRIMARY, 2)
        thresh_str = f"Thr: {thresh}"
        d.text(thresh_str, W - len(thresh_str) * 12 - 10, 130, TEXT_DIM, 2)

        self._last_vu_rms = rms

    def _draw_rec_status_full(self):
        """Draw recording status area (y=155..200)."""
        d = self.display
        if self.recorder.is_recording:
            # Red dot
            d.circle(30, 175, 8, REC_RED, True)
            dur = self.recorder.current_clip_duration
            d.text(f"RECORDING  {dur:05.1f}s", 50, 167, REC_RED, 2)
        else:
            d.text("Listening...", 50, 167, TEXT_DIM, 2)
        self._last_rec_state = self.recorder.state

    def _draw_stats(self):
        """Draw session statistics (y=218..240)."""
        d = self.display
        count = self.recorder.clip_count
        total = self.recorder.total_duration
        mins = int(total) // 60
        secs = int(total) % 60
        d.text(f"Clips: {count}", 10, 222, TEXT_PRIMARY, 2)
        d.text(f"Total: {mins}m {secs:02d}s", W // 2, 222, TEXT_PRIMARY, 2)

    def _draw_clips_list(self):
        """Draw recent clips list (y=258..440)."""
        d = self.display
        d.text("Recent Clips:", 10, 260, ACCENT, 2)

        clips = self._get_recent_clips(6)
        if not clips:
            d.text("No clips yet", 30, 290, TEXT_DIM, 2)
            return

        y = 285
        for fname, dur in clips:
            # Parse timestamp from filename: YYYYMMDD_HHMMSS.wav
            try:
                ts = fname.replace('.wav', '')
                time_str = f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
            except:
                time_str = fname[:15]
            d.text(time_str, 20, y, TEXT_PRIMARY, 2)
            d.text(f"{dur:.1f}s", W - 80, y, TEXT_DIM, 2)
            y += 25
            if y > 435:
                break

    def _draw_buttons(self):
        """Draw bottom button bar (y=455..495)."""
        d = self.display
        btn_y = 458
        btn_h = 38
        btn_w = 110
        gap = (W - 3 * btn_w) // 4

        labels = ["Thresh -", "SETTINGS", "Thresh +"]
        for i, label in enumerate(labels):
            bx = gap + i * (btn_w + gap)
            d.rect(bx, btn_y, btn_w, btn_h, BTN_BG, True)
            lw = len(label) * 8
            d.text(label, bx + (btn_w - lw) // 2,
                   btn_y + (btn_h - 8) // 2, ACCENT, 1)

    # ─── Drawing: Settings Screen ─────────────────────────────────

    def _draw_settings(self):
        """Draw settings overlay."""
        d = self.display
        d.fill(BG)

        # Title
        d.text("SETTINGS", 10, 15, ACCENT, 2)
        d.text("X", W - 25, 15, RED, 2)
        d.hline(0, 40, W, SEPARATOR)

        y = 60
        for item in self._settings_items:
            val = item['get']()
            unit = "dB" if "Gain" in item['label'] else ""
            d.text(item['label'], 20, y, TEXT_PRIMARY, 2)
            d.text(f"{val}{unit}", 200, y, GREEN, 2)

            # - button
            d.rect(20, y + 30, 80, 35, BTN_BG, True)
            d.text("-", 52, y + 38, TEXT_PRIMARY, 2)

            # + button
            d.rect(130, y + 30, 80, 35, BTN_BG, True)
            d.text("+", 162, y + 38, TEXT_PRIMARY, 2)

            y += 90

        # Back instruction
        d.text("Tap X to close", 120, H - 40, TEXT_DIM, 2)

        d.show()

    # ─── Partial updates (efficient) ──────────────────────────────

    def _update_vu_meter(self):
        """Partial redraw of VU meter bar only."""
        rms = self.recorder.current_rms
        thresh = self.recorder.trigger_threshold

        # Only update if level changed meaningfully
        if abs(rms - self._last_vu_rms) < 100:
            return

        d = self.display
        bar_x = 10
        bar_y = 105
        bar_w = W - 20
        bar_h = 20

        # Clear bar area
        d.rect(bar_x, bar_y, bar_w, bar_h, BAR_BG, True)

        # Fill bar
        max_rms = thresh * 2
        fill_w = min(bar_w, rms * bar_w // max(max_rms, 1))
        if fill_w > 0:
            color = GREEN
            if rms >= thresh:
                color = RED
            elif rms >= thresh * 2 // 3:
                color = ORANGE
            d.rect(bar_x, bar_y, fill_w, bar_h, color, True)

        # Threshold marker
        marker_x = bar_x + (thresh * bar_w // max(max_rms, 1))
        if bar_x < marker_x < bar_x + bar_w:
            d.vline(marker_x, bar_y, bar_h, TEXT_PRIMARY)

        # Update number
        d.rect(10, 130, 150, 16, BG, True)
        d.text(f"{rms}", 10, 130, TEXT_PRIMARY, 2)

        d.show_region(0, 100, W, 50)
        self._last_vu_rms = rms

    def _update_rec_status(self):
        """Partial redraw of recording status area."""
        d = self.display
        state = self.recorder.state

        # Clear status area
        d.rect(0, 155, W, 50, BG, True)

        if self.recorder.is_recording:
            # Blinking red dot
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_blink_time) > 500:
                self._rec_blink = not self._rec_blink
                self._last_blink_time = now

            if self._rec_blink:
                d.circle(30, 175, 8, REC_RED, True)
            dur = self.recorder.current_clip_duration
            d.text(f"RECORDING  {dur:05.1f}s", 50, 167, REC_RED, 2)
        else:
            d.text("Listening...", 50, 167, TEXT_DIM, 2)

        d.show_region(0, 150, W, 55)

        # If state just changed to idle, update clips list + stats
        if self._last_rec_state != state and state == 'idle':
            self._sd_free_mb = self._get_sd_free_mb()
            self._needs_redraw = True
        self._last_rec_state = state

    # ─── Touch handling ───────────────────────────────────────────

    def _handle_touch(self):
        """Process touch input."""
        point = self._read_touch()
        if point is None:
            return

        self._power.activity()

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_touch_time) < 300:
            return
        self._last_touch_time = now

        x, y = point

        if self._screen == SCREEN_MAIN:
            self._handle_main_touch(x, y)
        elif self._screen == SCREEN_SETTINGS:
            self._handle_settings_touch(x, y)

    def _handle_main_touch(self, x, y):
        """Touch handler for main screen."""
        btn_y = 458
        btn_h = 38
        btn_w = 110
        gap = (W - 3 * btn_w) // 4

        if btn_y <= y <= btn_y + btn_h:
            for i in range(3):
                bx = gap + i * (btn_w + gap)
                if bx <= x <= bx + btn_w:
                    if i == 0:  # Thresh -
                        self.recorder.set_threshold(
                            self.recorder.trigger_threshold - 500)
                        self._needs_redraw = True
                    elif i == 1:  # Settings
                        self._screen = SCREEN_SETTINGS
                        self._needs_redraw = True
                    elif i == 2:  # Thresh +
                        self.recorder.set_threshold(
                            self.recorder.trigger_threshold + 500)
                        self._needs_redraw = True
                    return

    def _handle_settings_touch(self, x, y):
        """Touch handler for settings screen."""
        # Close button (top right)
        if y < 40 and x > W - 50:
            self._screen = SCREEN_MAIN
            self._needs_redraw = True
            return

        # Check setting rows
        row_y = 60
        for item in self._settings_items:
            btn_top = row_y + 30
            btn_bot = btn_top + 35

            if btn_top <= y <= btn_bot:
                val = item['get']()
                if 20 <= x <= 100:  # Minus
                    new_val = max(item['min'], val - item['step'])
                    item['set'](new_val)
                    self._needs_redraw = True
                    return
                elif 130 <= x <= 210:  # Plus
                    new_val = min(item['max'], val + item['step'])
                    item['set'](new_val)
                    self._needs_redraw = True
                    return

            row_y += 90

    # ─── Main loop ────────────────────────────────────────────────

    def run(self):
        """Main loop — audio monitoring with optional display sleep.

        Audio recording continues even when the display is off.
        Press Ctrl+C to stop.
        """
        print("Starting Dog Audio Monitor...")
        self.draw()

        try:
            while True:
                # ALWAYS poll audio, regardless of display state
                self.recorder.poll()

                if self._power.is_display_on:
                    # ── ACTIVE PHASE: display on ──
                    self._handle_touch()

                    if self._needs_redraw:
                        self.draw()
                    elif self._screen == SCREEN_MAIN:
                        # Partial updates for live data
                        self._update_vu_meter()
                        self._update_rec_status()

                    # Display timeout (but audio keeps running)
                    if self._power.check_timeout():
                        gc.collect()

                    time.sleep_ms(30)

                else:
                    # ── DISPLAY OFF: audio still active ──
                    # Check for touch to wake display (INT pin active LOW)
                    if not self._touch_int():
                        # Debounce: wait and re-check
                        time.sleep_ms(20)
                        if not self._touch_int():
                            self._power.wake_display()
                            self._needs_redraw = True
                            self._last_touch_time = time.ticks_ms()

                    time.sleep_ms(30)

        except KeyboardInterrupt:
            print("\nDog Monitor stopped.")
            self.recorder.deinit()
