"""
main.py - ESP32-S3 Watch entry point.
Launches the watch UI with full error recovery.
Works standalone on battery (no WiFi/USB dependency).
Logs events and crashes to SD card via logger.

To stop and get a REPL: press Ctrl+C in Thonny/mpremote
Then restart with:
    from watch_ui import WatchUI
    ui = WatchUI()
    ui.run()
"""

import gc
import sys


def main():
    gc.collect()

    # Initialize logger early (before UI) so crashes get logged
    from logger import log
    log.init()
    log.info("Watch booting")

    try:
        from watch_ui import WatchUI
        ui = WatchUI(log=log)
        log.info("Watch UI started")
        ui.run()
    except KeyboardInterrupt:
        log.info("Watch stopped by user (Ctrl+C)")
        print("Watch stopped by user.")
    except Exception as e:
        _log_crash(log, e)
        _show_error(e)
        raise


def _log_crash(log, e):
    """Log the full crash info to SD card."""
    try:
        import io
        buf = io.StringIO()
        sys.print_exception(e, buf)
        tb = buf.getvalue()
        log.error(f"CRASH: {type(e).__name__}: {e}")
        for line in tb.strip().split("\n"):
            log.error(f"  {line}")
    except Exception:
        pass  # Don't crash while logging a crash


def _show_error(e):
    """Attempt to display a crash report on screen."""
    try:
        from drivers.co5300 import CO5300
        import board_config as BOARD

        display = CO5300()
        display.init()
        display.fill(BOARD.COLOR_BLACK)
        display.text("CRASH", 140, 50, BOARD.COLOR_RED, 3)

        err_type = type(e).__name__
        err_msg = str(e)
        display.text(err_type, 20, 130, BOARD.COLOR_YELLOW, 2)

        # Word-wrap error message (~50 chars per line at scale 1)
        y = 170
        for i in range(0, len(err_msg), 48):
            chunk = err_msg[i:i + 48]
            display.text(chunk, 20, y, BOARD.COLOR_WHITE)
            y += 12
            if y > 420:
                break

        display.text("Check /sd/logs/watch.log", 20, 440,
                     BOARD.COLOR_GRAY)
        display.text("Connect USB for full traceback", 20, 460,
                     BOARD.COLOR_GRAY)
        display.show()
    except Exception:
        pass  # Display init itself failed — nothing we can do


main()
