"""
alert_manager.py
Fires native OS sound + desktop notification when MaaNiish Arrow triggers.
Also queues alerts so the Streamlit UI can inject an in-browser sound.
"""
import logging
import os
import sys
import threading
from collections import deque
from typing import List

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  OS-level sound (fires immediately in background thread)            #
# ------------------------------------------------------------------ #

def _play_sound():
    try:
        if sys.platform == "darwin":
            # macOS: use built-in afplay (no extra install needed)
            os.system("afplay /System/Library/Sounds/Glass.aiff")
        elif sys.platform == "win32":
            import winsound  # type: ignore
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            # Linux: try paplay then fallback to terminal bell
            ret = os.system("paplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null")
            if ret != 0:
                print("\a", end="", flush=True)
    except Exception as exc:
        logger.debug("_play_sound: %s", exc)


# ------------------------------------------------------------------ #
#  Desktop notification                                               #
# ------------------------------------------------------------------ #

def _show_notification(signal: dict):
    try:
        from plyer import notification  # type: ignore

        body = (
            f"{signal['index']} {int(signal['strike'])} {signal['option_type']}\n"
            f"1m Candle  {signal['candle_timestamp'].strftime('%H:%M')}\n"
            f"Candle High : {signal['candle_high']:.2f}\n"
            f"Top Bid     : {signal['top_bid']:.2f}  (+{signal['excess']:.2f})"
        )
        notification.notify(
            title="▼ MaaNiish Arrow",
            message=body,
            app_name="MaaNiish Arrow",
            timeout=8,
        )
    except Exception as exc:
        logger.debug("_show_notification: %s", exc)


# ------------------------------------------------------------------ #
#  AlertManager                                                       #
# ------------------------------------------------------------------ #

class AlertManager:
    def __init__(self):
        self._lock    = threading.Lock()
        # Queue consumed by the Streamlit UI to inject in-browser sound
        self._pending: deque = deque(maxlen=100)

    def fire(self, signal: dict):
        """Called by data_feed when a signal is confirmed."""
        # Play OS sound immediately (non-blocking)
        threading.Thread(target=_play_sound,            daemon=True).start()
        # Show desktop popup immediately (non-blocking)
        threading.Thread(target=_show_notification, args=(signal,), daemon=True).start()

        with self._lock:
            self._pending.append(signal)

    def pop_pending(self) -> List[dict]:
        """Drain and return pending alerts for the UI."""
        with self._lock:
            result = list(self._pending)
            self._pending.clear()
            return result
