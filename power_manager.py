"""
Power Manager for ESP32-S3 Watch
Orchestrates display sleep/wake, light sleep, and wake sources
(touch interrupt + timer-based IMU polling).

Usage:
    from power_manager import PowerManager
    pm = PowerManager(display, imu, touch_int_pin)

    # In active loop:
    pm.activity()        # Reset timeout on user interaction
    pm.check_timeout()   # Returns True if display just went to sleep

    # In sleep loop:
    cause = pm.enter_light_sleep()  # 'touch', 'motion', or 'timer'
"""

import time
import machine

try:
    import esp32
    _HAS_ESP32 = True
except ImportError:
    _HAS_ESP32 = False


# Wake reason constants (MicroPython esp32 port)
_PIN_WAKE   = 2
_TIMER_WAKE = 4


class PowerManager:
    """Manages display timeout and light-sleep cycling."""

    DISPLAY_TIMEOUT_MS = 10_000   # 10s before display sleeps
    POLL_INTERVAL_MS   = 3_000    # 3s between IMU polls during sleep
    MOTION_THRESHOLD   = 3000     # Raw accel delta (~0.37g at 4G)

    def __init__(self, display, imu, touch_int_pin, touch_gpio_num=38):
        """
        Args:
            display:        CO5300 display instance
            imu:            QMI8658 instance (or None if IMU unavailable)
            touch_int_pin:  machine.Pin for touch interrupt (GPIO38)
            touch_gpio_num: GPIO number for touch INT (for wake bitmask)
        """
        self.display = display
        self.imu = imu
        self._touch_pin = touch_int_pin
        self._touch_gpio = touch_gpio_num
        self._display_on = True
        self._last_activity = time.ticks_ms()
        self._wake_configured = False

        # Configure GPIO wake source once at init
        # ESP32-S3: GPIO38 is NOT an RTC GPIO, so wake_on_ext0 won't work.
        # Use wake_on_ext1 (bitmask-based) which supports all GPIOs.
        if _HAS_ESP32:
            try:
                if hasattr(esp32, 'wake_on_ext1'):
                    bitmask = 1 << self._touch_gpio
                    esp32.wake_on_ext1(pins=(self._touch_pin,), level=esp32.WAKEUP_ALL_LOW)
                    self._wake_configured = True
                    print("Power: touch wake configured (ext1)")
                elif hasattr(esp32, 'wake_on_ext0'):
                    esp32.wake_on_ext0(self._touch_pin, esp32.WAKEUP_ALL_LOW)
                    self._wake_configured = True
                    print("Power: touch wake configured (ext0)")
            except ValueError:
                print("Power: GPIO wake not supported, using timer-only")
            except Exception as e:
                print(f"Power: wake config error: {e}")

    def activity(self):
        """Call on any user interaction to reset the display timeout."""
        self._last_activity = time.ticks_ms()
        if not self._display_on:
            self.wake_display()

    def wake_display(self):
        """Turn display back on from sleep."""
        self.display.wake()
        self._display_on = True
        self._last_activity = time.ticks_ms()

    def sleep_display(self):
        """Put display into low-power sleep mode."""
        self.display.sleep()
        self._display_on = False

    def check_timeout(self):
        """Check if display should sleep due to inactivity.
        Returns True if the display just went to sleep.
        """
        if not self._display_on:
            return False
        elapsed = time.ticks_diff(time.ticks_ms(), self._last_activity)
        if elapsed >= self.DISPLAY_TIMEOUT_MS:
            self.sleep_display()
            return True
        return False

    def enter_light_sleep(self):
        """Enter light sleep with touch + timer wake sources.

        Returns:
            'touch'  — woke from touch screen tap
            'motion' — woke from timer, IMU detected motion
            'timer'  — woke from timer, no motion (stay asleep)
        """
        # Enter light sleep — wakes on touch (if configured) OR after POLL_INTERVAL_MS
        machine.lightsleep(self.POLL_INTERVAL_MS)

        # Brief settle time for I2C bus after wake
        time.sleep_ms(2)

        # Determine wake cause
        cause = machine.wake_reason()

        if cause == _PIN_WAKE:
            self.wake_display()
            return 'touch'

        if cause == _TIMER_WAKE:
            if self.imu is not None:
                try:
                    if self.imu.detect_motion(self.MOTION_THRESHOLD):
                        self.wake_display()
                        return 'motion'
                except Exception:
                    pass  # IMU read failure — stay asleep
            return 'timer'

        # Unknown wake cause — treat as touch (safe default)
        self.wake_display()
        return 'touch'

    @property
    def is_display_on(self):
        """True if the display is currently awake."""
        return self._display_on
