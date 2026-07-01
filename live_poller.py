"""
live_poller.py
REST-based live price polling (every 5 seconds).

Runs alongside the WebSocket feed to:
  1. Update LTP for all subscribed option instruments every 5 s
  2. Update live spot prices (NIFTY / BANKNIFTY / SENSEX) every 5 s
  3. Detect ATM shifts of ≥2 strikes and auto-resubscribe to new strikes
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pytz

from config import STRIKE_INTERVALS, INDEX_SECURITY_IDS, ATM_RANGE
from state import app_state

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class LivePoller:
    """
    Background thread that polls Dhan REST API every `interval` seconds.
    Supplements the WebSocket – if WS ticks are delayed this keeps prices fresh.
    Also monitors ATM shifts and triggers auto-resubscription.
    """

    def __init__(self, dhan, interval: int = 5):
        self._dhan          = dhan
        self._interval      = interval
        self._running       = False
        self._instruments:  List[dict]         = []
        self._feed_manager                      = None   # set via set_feed_manager()
        self._on_atm_shift: Optional[Callable] = None   # callback(idx, old, new)
        self._lock          = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def set_feed_manager(self, feed_manager):
        self._feed_manager = feed_manager

    def start(self, instruments: List[dict], on_atm_shift: Optional[Callable] = None):
        if self._running:
            return
        with self._lock:
            self._instruments  = list(instruments)
            self._on_atm_shift = on_atm_shift
            self._running      = True
        # Initialise ATM from first spot prices (may be 0 until first poll)
        if not hasattr(app_state, "current_atm"):
            app_state.current_atm = {}
        if not hasattr(app_state, "atm_shifted"):
            app_state.atm_shifted = {}
        for idx, step in STRIKE_INTERVALS.items():
            spot = app_state.spot_prices.get(idx, 0)
            if spot > 0:
                app_state.current_atm[idx] = int(round(spot / step) * step)

        threading.Thread(
            target=self._loop,
            daemon=True,
            name="LivePoller",
        ).start()
        logger.info("LivePoller: started — %d instruments, poll every %ds",
                    len(instruments), self._interval)

    def stop(self):
        self._running = False

    def update_instruments(self, instruments: List[dict]):
        """Replace instrument list after ATM resubscription."""
        with self._lock:
            self._instruments = list(instruments)
        logger.info("LivePoller: instrument list updated — %d instruments", len(instruments))

    # ------------------------------------------------------------------ #
    #  Main loop                                                           #
    # ------------------------------------------------------------------ #

    def _loop(self):
        while self._running:
            try:
                self._poll_quotes()
                self._check_atm_shift()
            except Exception as exc:
                logger.warning("LivePoller: poll error: %s", exc)
            time.sleep(self._interval)

    # ------------------------------------------------------------------ #
    #  Quote polling                                                       #
    # ------------------------------------------------------------------ #

    def _poll_quotes(self):
        with self._lock:
            instruments = list(self._instruments)

        # Build segment → [security_ids] map
        by_seg: Dict[str, List[str]] = {}
        for inst in instruments:
            by_seg.setdefault(inst["exchange_segment"], []).append(inst["security_id"])
        # Always include index instruments for live spot prices
        for info in INDEX_SECURITY_IDS.values():
            by_seg.setdefault(info["exchange_segment"], []).append(info["security_id"])

        try:
            resp = self._dhan.ticker_data(by_seg)
        except Exception as exc:
            logger.warning("LivePoller: ticker_data error: %s", exc)
            return

        if not isinstance(resp, dict) or resp.get("status") != "success":
            logger.debug("LivePoller: bad response: %s", str(resp)[:200])
            return

        now      = datetime.now(IST)
        inst_map = {i["security_id"]: i for i in instruments}
        idx_map  = {info["security_id"]: name for name, info in INDEX_SECURITY_IDS.items()}

        for records in resp.get("data", {}).values():
            if not isinstance(records, list):
                continue
            for rec in records:
                sid = str(rec.get("security_id", "")).strip()
                ltp = float(rec.get("last_price", 0) or 0)
                if ltp <= 0:
                    continue

                # ── Spot price (index) ──────────────────────────────────
                if sid in idx_map:
                    app_state.update_spot_price(idx_map[sid], ltp)

                # ── Option LTP → update current candle ─────────────────
                if sid in inst_map:
                    cur = app_state.current_candles.get(sid)
                    ts  = now.replace(second=0, microsecond=0)
                    if cur:
                        updated = dict(cur)
                        updated["close"] = ltp
                        updated["high"]  = max(updated.get("high", ltp), ltp)
                        updated["low"]   = min(updated.get("low",  ltp), ltp)
                        app_state.update_current_candle(sid, updated)
                    else:
                        # No WebSocket candle yet — seed one from REST
                        app_state.update_current_candle(sid, {
                            "timestamp":    ts,
                            "open":         ltp,
                            "high":         ltp,
                            "low":          ltp,
                            "close":        ltp,
                            "top_bid_high": 0.0,
                            "signal":       False,
                            "signal_bid":   None,
                            "closed":       False,
                        })
                    app_state.last_update = now

        logger.debug("LivePoller: quotes updated for %d options", len(inst_map))

    # ------------------------------------------------------------------ #
    #  ATM shift detection & auto-resubscription                          #
    # ------------------------------------------------------------------ #

    def _check_atm_shift(self):
        _cur_atm  = getattr(app_state, "current_atm",  {})
        _atm_shft = getattr(app_state, "atm_shifted", {})
        for idx, step in STRIKE_INTERVALS.items():
            spot = app_state.spot_prices.get(idx, 0)
            if spot <= 0:
                continue
            new_atm = int(round(spot / step) * step)
            old_atm = _cur_atm.get(idx, 0)

            if old_atm == 0:
                _cur_atm[idx] = new_atm
                continue

            shift = abs(new_atm - old_atm) // step
            if shift >= 2:   # ATM moved ≥2 strikes → resubscribe
                logger.info(
                    "LivePoller: %s ATM %d → %d  (shift=%+d strikes)",
                    idx, old_atm, new_atm, (new_atm - old_atm) // step,
                )
                _cur_atm[idx]  = new_atm
                _atm_shft[idx] = True

                # Auto-resubscribe in a separate thread so this loop isn't blocked
                threading.Thread(
                    target=self._resubscribe,
                    args=(idx,),
                    daemon=True,
                    name=f"ATMResub-{idx}",
                ).start()
                if self._on_atm_shift:
                    try:
                        self._on_atm_shift(idx, old_atm, new_atm)
                    except Exception as exc:
                        logger.error("LivePoller: on_atm_shift callback error: %s", exc)

    def _resubscribe(self, changed_idx: str):
        """
        Rebuild full instrument list based on current spot prices and
        register any new strikes with the WebSocket feed manager.
        """
        try:
            from instrument_manager import InstrumentManager
            mgr = InstrumentManager()
            mgr.load_master()
            spot_prices = dict(app_state.spot_prices)
            new_instruments = mgr.build_subscriptions(spot_prices, ATM_RANGE)
            if not new_instruments:
                logger.warning("LivePoller: resubscribe produced 0 instruments for %s", changed_idx)
                return

            # Register new instruments in state + feed
            for inst in new_instruments:
                app_state.add_instrument(inst)
            if self._feed_manager:
                self._feed_manager.resubscribe(new_instruments)

            # Update poller's own instrument list
            self.update_instruments(new_instruments)
            getattr(app_state, "atm_shifted", {})[changed_idx] = False   # cleared after resub
            logger.info(
                "LivePoller: resubscribed %d instruments after %s ATM shift",
                len(new_instruments), changed_idx,
            )
        except Exception as exc:
            logger.error("LivePoller: resubscribe error for %s: %s", changed_idx, exc)


# ── Module-level singleton ────────────────────────────────────────────────────
# Stored here (not in st.session_state) so it survives Streamlit hot-reloads.
_instance: Optional[LivePoller] = None


def get_instance() -> Optional["LivePoller"]:
    """Return the running LivePoller, or None if not yet started."""
    return _instance


def set_instance(poller: "LivePoller") -> None:
    global _instance
    _instance = poller
