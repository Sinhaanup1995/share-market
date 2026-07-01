"""
data_feed.py
Connects to Dhan's live WebSocket market feed (Full Feed = top-5 depth),
pipes ticks into CandleBuilder per instrument, and runs SignalDetector.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import pytz

from alert_manager import AlertManager
from candle_builder import CandleBuilder
from signal_detector import SignalDetector
from state import app_state

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# Backoff delays in seconds for each retry attempt (conservative — avoids 429)
_RETRY_DELAYS = [30, 60, 120, 300, 600]


class DhanFeedManager:
    """
    Wraps dhanhq.marketfeed.MarketFeed (which manages its own thread via start()).
    One instance per app session.
    """

    def __init__(self, client_id: str, access_token: str):
        self._client_id     = client_id
        self._access_token  = access_token
        self._builders:    Dict[str, CandleBuilder] = {}
        self._inst_map:    Dict[str, dict]           = {}
        self._alert_mgr    = AlertManager()
        self._detector     = SignalDetector(self._on_signal)
        self._feed         = None

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def start(self, instruments: List[dict]):
        """Register instruments and launch a daemon thread that connects
        with exponential-backoff retry on 429 / transient errors."""
        self._register_instruments(instruments)

        # Also subscribe index spot instruments so live spot price updates flow in.
        # These are NOT in _inst_map so _on_tick routes them to _handle_index_tick.
        from config import INDEX_SECURITY_IDS
        index_insts = [
            {"security_id": info["security_id"], "exchange_segment": "IDX_I"}
            for info in INDEX_SECURITY_IDS.values()
        ]
        self._instruments = list(instruments) + index_insts   # full sub list

        t = threading.Thread(
            target=self._connect_loop,
            daemon=True,
            name="DhanFeedLoop",
        )
        t.start()
        logger.info("DhanFeedManager: feed loop thread started")

    def _connect_loop(self):
        """Keep connecting (with backoff) until success."""
        from dhanhq import marketfeed, DhanContext  # type: ignore

        ctx      = DhanContext(self._client_id, self._access_token)
        sub_list = self._build_sub_list(self._instruments, marketfeed)
        if not sub_list:
            app_state.set_connected(False, "No instruments to subscribe")
            return

        attempt = 0
        while True:
            try:
                logger.info("DhanFeedManager: connect attempt %d (%d instruments)",
                            attempt + 1, len(sub_list))
                app_state.set_connected(False, "Connecting…")

                self._feed = marketfeed.MarketFeed(
                    dhan_context=ctx,
                    instruments=sub_list,
                    version="v2",
                    on_ticks=self._on_tick,
                )
                app_state.set_connected(True)
                logger.info("DhanFeedManager: WebSocket connected")

                # run() is blocking – returns when the connection drops
                self._feed.run()

                # If we get here the connection closed normally
                logger.warning("DhanFeedManager: feed disconnected, will reconnect")
                app_state.set_connected(False, "Disconnected – reconnecting…")
                attempt = 0          # reset backoff on clean disconnect

            except Exception as exc:
                err = str(exc)
                logger.error("DhanFeedManager: connection error: %s", err)

                if "429" in err:
                    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    msg   = f"Rate-limited by Dhan (HTTP 429). Retrying in {delay}s…"
                    logger.warning(msg)
                    app_state.set_connected(False, msg)
                else:
                    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    app_state.set_connected(False, f"Error: {err}. Retrying in {delay}s…")

                attempt += 1
                time.sleep(delay)

    def is_alive(self) -> bool:
        return self._feed is not None

    def resubscribe(self, instruments: List[dict]):
        """
        Register new instruments that appeared after an ATM shift.
        Tries dynamic WebSocket subscribe first; falls back to registering
        so they are active on the next reconnect.
        """
        from config import INDEX_SECURITY_IDS
        new = [i for i in instruments if i["security_id"] not in self._inst_map]
        if new:
            self._register_instruments(new)
            logger.info("DhanFeedManager.resubscribe: %d new instruments registered", len(new))

        # Rebuild full sub list (options + index spots)
        index_insts = [
            {"security_id": info["security_id"], "exchange_segment": "IDX_I"}
            for info in INDEX_SECURITY_IDS.values()
        ]
        self._instruments = list(instruments) + index_insts

        # Try dynamic subscribe if the feed object supports it
        if self._feed and new:
            try:
                from dhanhq import marketfeed
                sub_list = self._build_sub_list(new, marketfeed)
                if hasattr(self._feed, "subscribe"):
                    self._feed.subscribe(sub_list)
                    logger.info("DhanFeedManager: dynamically subscribed %d instruments", len(new))
            except Exception as exc:
                logger.warning("DhanFeedManager: dynamic subscribe failed (%s) – active on reconnect", exc)

    def pop_pending_alerts(self):
        return self._alert_mgr.pop_pending()

    # ------------------------------------------------------------------ #
    #  Instrument setup                                                    #
    # ------------------------------------------------------------------ #

    def _register_instruments(self, instruments: List[dict]):
        for inst in instruments:
            sid = inst["security_id"]
            self._inst_map[sid]    = inst
            self._builders[sid]    = CandleBuilder(sid)
            app_state.add_instrument(inst)

    # ------------------------------------------------------------------ #
    #  Subscription list builder                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_sub_list(instruments: List[dict], marketfeed) -> list:
        """Convert our instrument dicts to dhanhq subscription tuples."""
        # Correct segment integers from MarketFeed source:
        # IDX=0, NSE=1, NSE_FNO=2, NSE_CURR=3, BSE=4, MCX=5, BSE_FNO=8
        seg_map = {
            "NSE_FNO": getattr(marketfeed.MarketFeed, "NSE_FNO", 2),
            "BSE_FNO": getattr(marketfeed.MarketFeed, "BSE_FNO", 8),
            "NSE":     getattr(marketfeed.MarketFeed, "NSE",     1),
            "BSE":     getattr(marketfeed.MarketFeed, "BSE",     4),
            "IDX_I":   getattr(marketfeed.MarketFeed, "IDX",     0),
        }
        full_code = getattr(marketfeed.MarketFeed, "Full", 21)
        result = []
        for inst in instruments:
            seg = seg_map.get(inst.get("exchange_segment", "NSE_FNO"), 2)
            result.append((seg, inst["security_id"], full_code))
        return result

    # ------------------------------------------------------------------ #
    #  Tick processing                                                     #
    # ------------------------------------------------------------------ #

    def _on_tick(self, tick: dict):
        try:
            security_id = str(tick.get("security_id", "")).strip()

            # ── Spot-index tick (not in our options list) ─────────────
            if security_id not in self._inst_map:
                self._handle_index_tick(security_id, tick)
                return

            ltp = float(tick.get("LTP", 0) or 0)
            if ltp <= 0:
                return

            # Extract Top Bid (best bid price, level 0)
            top_bid = 0.0
            depth   = tick.get("depth", {})
            if depth and depth.get("buy"):
                top_bid = float(depth["buy"][0].get("price", 0) or 0)

            timestamp = self._parse_timestamp(tick.get("LTT", ""))

            builder = self._builders[security_id]
            current_candle, closed_candle = builder.process_tick(ltp, top_bid, timestamp)

            # Persist to shared state
            app_state.update_current_candle(security_id, current_candle)
            if closed_candle:
                app_state.close_candle(security_id, closed_candle)

            # Signal check
            instrument = self._inst_map[security_id]
            fired = self._detector.check(security_id, instrument, current_candle, top_bid)
            if fired:
                # Mark the candle so the chart can show the arrow
                builder.mark_signal(top_bid)
                app_state.update_current_candle(security_id, current_candle)

        except Exception as exc:
            logger.error("_on_tick error: %s", exc, exc_info=True)

    def _handle_index_tick(self, security_id: str, tick: dict):
        """Update spot price so the UI can show current ATM."""
        from config import INDEX_SECURITY_IDS
        ltp = float(tick.get("LTP", 0) or 0)
        if ltp <= 0:
            return
        for index, info in INDEX_SECURITY_IDS.items():
            if info["security_id"] == security_id:
                app_state.update_spot_price(index, ltp)
                break

    def _on_signal(self, signal: dict):
        app_state.add_signal(signal)
        self._alert_mgr.fire(signal)

    # ------------------------------------------------------------------ #
    #  Timestamp parsing                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_timestamp(ltt: str) -> datetime:
        """
        Dhan LTT format is 'HH:MM:SS'.
        We combine it with today's date in IST.
        """
        now = datetime.now(IST)
        try:
            if ltt and ":" in ltt:
                parts = ltt.split(":")
                return now.replace(
                    hour=int(parts[0]),
                    minute=int(parts[1]),
                    second=int(parts[2]) if len(parts) > 2 else 0,
                    microsecond=0,
                )
        except (ValueError, IndexError):
            pass
        return now
