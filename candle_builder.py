"""
candle_builder.py
Builds 1-minute OHLC candles from individual price ticks.
Also tracks the rolling maximum Top-Bid seen inside each candle.
"""
from datetime import datetime
from typing import Optional, Tuple

import pytz

IST = pytz.timezone("Asia/Kolkata")


def _minute_floor(dt: datetime) -> datetime:
    """Truncate a datetime to its minute boundary (IST-aware)."""
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.replace(second=0, microsecond=0)


class CandleBuilder:
    """
    One instance per subscribed security.

    process_tick() returns:
        (current_candle, closed_candle | None)

    closed_candle is set only when the tick belongs to a NEW minute,
    causing the previous candle to be finalized.
    """

    def __init__(self, security_id: str):
        self.security_id       = security_id
        self._candle: Optional[dict] = None
        self._top_bid: float   = 0.0

    # ------------------------------------------------------------------ #

    def process_tick(
        self,
        ltp: float,
        top_bid: float,
        timestamp: datetime,
    ) -> Tuple[dict, Optional[dict]]:
        """
        Returns (current_candle, closed_candle).
        closed_candle is non-None when this tick opened a new minute.
        """
        ts = _minute_floor(timestamp)
        closed: Optional[dict] = None

        if self._candle is None:
            # First tick ever
            self._candle  = self._new_candle(ts, ltp)
            self._top_bid = top_bid

        elif ts > self._candle["timestamp"]:
            # New minute – close old candle
            closed = self._finalise()
            # Start fresh
            self._candle  = self._new_candle(ts, ltp)
            self._top_bid = top_bid

        else:
            # Same minute – update running values
            self._candle["high"]  = max(self._candle["high"],  ltp)
            self._candle["low"]   = min(self._candle["low"],   ltp)
            self._candle["close"] = ltp
            self._top_bid         = max(self._top_bid, top_bid)
            self._candle["top_bid_high"] = self._top_bid

        return self._candle, closed

    def mark_signal(self, bid_price: float):
        """Called by SignalDetector when MaaNiish Arrow fires on this candle."""
        if self._candle:
            self._candle["signal"]     = True
            self._candle["signal_bid"] = bid_price

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _new_candle(ts: datetime, open_price: float) -> dict:
        return {
            "timestamp":    ts,
            "open":         open_price,
            "high":         open_price,
            "low":          open_price,
            "close":        open_price,
            "top_bid_high": 0.0,   # max Top-Bid seen this minute
            "signal":       False,
            "signal_bid":   None,
            "closed":       False,
        }

    def _finalise(self) -> dict:
        candle          = dict(self._candle)
        candle["closed"]       = True
        candle["top_bid_high"] = self._top_bid
        return candle
