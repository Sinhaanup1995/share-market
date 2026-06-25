"""
signal_detector.py
MaaNiish Arrow logic (non-repainting):

  During a 1-minute candle, if ANY Top Bid > that candle's running High
  → fire once and never again for that candle.
"""
import logging
from datetime import datetime
from typing import Callable, Dict, Set

logger = logging.getLogger(__name__)


class SignalDetector:
    """
    One shared instance used by the data feed.

    check() is called on *every* tick for every subscribed security.
    It fires on_signal() at most once per (security_id, candle_timestamp).
    """

    def __init__(self, on_signal: Callable[[dict], None]):
        self._on_signal = on_signal
        # security_id -> set of candle timestamps that already fired
        self._fired: Dict[str, Set[datetime]] = {}

    def check(
        self,
        security_id: str,
        instrument: dict,
        current_candle: dict,
        top_bid: float,
    ) -> bool:
        """
        Returns True if a new signal fired on this tick.
        Non-repainting: each (security_id, candle_ts) fires at most once.
        """
        if not current_candle or top_bid <= 0:
            return False

        candle_high = current_candle["high"]
        candle_ts   = current_candle["timestamp"]

        # Guard: already fired for this candle?
        fired_set = self._fired.setdefault(security_id, set())
        if candle_ts in fired_set:
            return False

        # ── THE SIGNAL CONDITION ──────────────────────────────────────
        if top_bid > candle_high:
            fired_set.add(candle_ts)

            signal = {
                "fired_at":        datetime.now(),
                "candle_timestamp": candle_ts,
                "security_id":     security_id,
                "trading_symbol":  instrument.get("trading_symbol", security_id),
                "index":           instrument.get("index", ""),
                "strike":          instrument.get("strike_price", 0),
                "option_type":     instrument.get("option_type", ""),
                "candle_open":     current_candle["open"],
                "candle_high":     candle_high,
                "candle_low":      current_candle["low"],
                "candle_close":    current_candle["close"],
                "top_bid":         top_bid,
                "excess":          round(top_bid - candle_high, 2),
            }

            logger.info(
                "[MaaNiish Arrow] %s | Candle High=%.2f | Top Bid=%.2f | +%.2f | %s",
                signal["trading_symbol"],
                candle_high,
                top_bid,
                signal["excess"],
                candle_ts.strftime("%H:%M"),
            )

            self._on_signal(signal)
            return True

        return False
