"""
main.py - ESP32-S3 Watch entry point.
Launches the watch UI with full error recovery.
Works standalone on battery (no WiFi/USB dependency).

To stop and get a REPL: press Ctrl+C in Thonny/mpremote
Then restart with:
    from watch_ui import WatchUI
    ui = WatchUI()
    ui.run()
"""

import gc


def main():
    gc.collect()

    try:
        from watch_ui import WatchUI
        ui = WatchUI()
        ui.run()
    except KeyboardInterrupt:
        print("Watch stopped by user.")
    except Exception as e:
        _show_error(e)
        raise


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

        display.text("Connect USB for full traceback", 20, 460,
                     BOARD.COLOR_GRAY)
        display.show()
    except Exception:
        pass  # Display init itself failed — nothing we can do


main()
