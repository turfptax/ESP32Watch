"""
SD Card Logger for ESP32-S3 Watch
Persistent logging to micro SD card with RAM fallback.

Writes timestamped log entries to /sd/logs/watch.log.
If no SD card is present, buffers entries in RAM (last 20).

Usage:
    from logger import log
    log.init()                    # Mount SD, start logging
    log.init(rtc=my_rtc)         # Use RTC for timestamps
    log.info("Watch booting")
    log.error("Something broke")
    print(log.read_last(5))      # Last 5 log lines
"""

import os
import time
from machine import Pin

import board_config as BOARD


_LOG_DIR  = "/sd/logs"
_LOG_FILE = "/sd/logs/watch.log"
_LOG_OLD  = "/sd/logs/watch.log.old"
_MAX_SIZE = 64 * 1024   # 64KB before rotation
_RAM_MAX  = 20           # Max RAM buffer entries when no SD


class Logger:
    """SD-card-backed logger with RAM fallback."""

    def __init__(self):
        self._sd = None
        self._sd_ok = False
        self._rtc = None
        self._ram_buf = []

    def init(self, rtc=None):
        """Mount SD card and prepare for logging.

        Args:
            rtc: PCF85063 RTC instance for timestamps (optional)
        """
        self._rtc = rtc
        self._mount_sd()

        if self._sd_ok:
            self._ensure_log_dir()
            self.info("Logger started (SD card)")
        else:
            self.info("Logger started (RAM only)")

    def _mount_sd(self):
        """Try to mount the SD card at /sd with retries."""
        # Check if already mounted
        try:
            os.stat("/sd")
            self._sd_ok = True
            return
        except OSError:
            pass

        # Mount SD card using slot from board_config
        from machine import SDCard

        try:
            self._sd = SDCard(
                slot=BOARD.SD_SLOT,
                sck=Pin(BOARD.SD_CLK),
                mosi=Pin(BOARD.SD_CMD),
                miso=Pin(BOARD.SD_DATA),
                cs=Pin(BOARD.SD_CS),
            )
            os.mount(self._sd, "/sd")
            self._sd_ok = True
            print("Logger: SD card mounted")
        except Exception as e:
            # Deinit SDCard to release the SPI host it claimed
            if self._sd is not None:
                try:
                    self._sd.deinit()
                except Exception:
                    pass
                self._sd = None
            print(f"Logger: SD mount failed ({e}), using RAM buffer")
            self._sd_ok = False

    def _ensure_log_dir(self):
        """Create /sd/logs/ directory if it doesn't exist."""
        try:
            os.stat(_LOG_DIR)
        except OSError:
            try:
                os.mkdir(_LOG_DIR)
            except Exception:
                pass

    def _timestamp(self):
        """Get a formatted timestamp string."""
        if self._rtc:
            try:
                dt = self._rtc.datetime()
                return f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} {dt[4]:02d}:{dt[5]:02d}:{dt[6]:02d}"
            except Exception:
                pass
        # Fallback to system time
        t = time.localtime()
        return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}:{t[5]:02d}"

    def _write(self, level, msg):
        """Write a log entry."""
        entry = f"{self._timestamp()} [{level}] {msg}"

        # Always buffer in RAM (for read_last when on Info screen)
        self._ram_buf.append(entry)
        if len(self._ram_buf) > _RAM_MAX:
            self._ram_buf.pop(0)

        # Write to SD if available
        if self._sd_ok:
            try:
                self._rotate_if_needed()
                with open(_LOG_FILE, "a") as f:
                    f.write(entry + "\n")
            except Exception as e:
                # SD write failed — don't crash the watch over logging
                print(f"Logger: write failed ({e})")

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds max size."""
        try:
            size = os.stat(_LOG_FILE)[6]
            if size > _MAX_SIZE:
                # Remove old backup, rename current to .old
                try:
                    os.remove(_LOG_OLD)
                except OSError:
                    pass
                os.rename(_LOG_FILE, _LOG_OLD)
        except OSError:
            pass  # File doesn't exist yet — that's fine

    # ─── Public API ───────────────────────────────────────────

    def info(self, msg):
        """Log an INFO level message."""
        self._write("INFO", msg)

    def warn(self, msg):
        """Log a WARN level message."""
        self._write("WARN", msg)

    def error(self, msg):
        """Log an ERROR level message."""
        self._write("ERROR", msg)

    def read_last(self, n=10):
        """Read the last N log entries.

        Returns from SD file if available, otherwise RAM buffer.
        """
        if self._sd_ok:
            try:
                with open(_LOG_FILE, "r") as f:
                    lines = f.readlines()
                return [l.strip() for l in lines[-n:]]
            except Exception:
                pass
        # Fallback to RAM buffer
        return self._ram_buf[-n:]

    @property
    def has_sd(self):
        """True if SD card is mounted and writable."""
        return self._sd_ok

    def deinit(self):
        """Unmount SD card."""
        if self._sd_ok:
            try:
                os.umount("/sd")
            except Exception:
                pass
            self._sd_ok = False


# ─── Module-level singleton ──────────────────────────────────
log = Logger()
