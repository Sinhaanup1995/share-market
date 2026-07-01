"""
state.py – Thread-safe shared application state.
All background threads write here; the Streamlit UI reads here.
"""
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional


class AppState:
    def __init__(self):
        self._lock = threading.Lock()

        # security_id -> instrument metadata dict
        self.instruments: Dict[str, dict] = {}

        # security_id -> list of *closed* 1-min candles (up to 200)
        self.candles: Dict[str, List[dict]] = defaultdict(list)

        # security_id -> current (in-progress) candle
        self.current_candles: Dict[str, dict] = {}

        # All fired signals (up to 500)
        self.signals: List[dict] = []

        # Ring-buffer of signals not yet consumed by the UI
        self._new_signals: deque = deque(maxlen=200)

        # Feed health
        self.connected: bool = False
        self.connection_error: Optional[str] = None

        self.last_update: Optional[datetime] = None

        # REST poller health
        self.last_poll_time: Optional[datetime] = None
        self.poll_count: int = 0

        # Current spot prices  index -> float
        self.spot_prices: Dict[str, float] = {}

        # Historical preload tracking
        self.history_loaded: bool = False
        self.history_candles_count: int = 0
        self.history_error: Optional[str] = None

        # Live ATM tracking (updated every 5 s by LivePoller)
        self.current_atm: Dict[str, int]  = {}   # index -> current ATM strike
        self.atm_shifted: Dict[str, bool] = {}   # index -> True when resub pending

    # ------------------------------------------------------------------ #
    #  Instrument registry                                                 #
    # ------------------------------------------------------------------ #
    def add_instrument(self, info: dict):
        with self._lock:
            self.instruments[info["security_id"]] = info

    def get_instruments(self) -> List[dict]:
        with self._lock:
            return list(self.instruments.values())

    def get_instruments_for_index(self, index: str) -> List[dict]:
        with self._lock:
            return [v for v in self.instruments.values() if v.get("index") == index]

    # ------------------------------------------------------------------ #
    #  Candle management                                                   #
    # ------------------------------------------------------------------ #
    def update_current_candle(self, security_id: str, candle: dict):
        with self._lock:
            self.current_candles[security_id] = candle
            self.last_update = datetime.now()

    def close_candle(self, security_id: str, candle: dict):
        with self._lock:
            bucket = self.candles[security_id]
            bucket.append(candle)
            if len(bucket) > 200:
                self.candles[security_id] = bucket[-200:]

    def get_candles(self, security_id: str) -> List[dict]:
        """Returns closed candles + current in-progress candle (copy)."""
        with self._lock:
            result = list(self.candles.get(security_id, []))
            cur = self.current_candles.get(security_id)
            if cur:
                result = result + [dict(cur)]
            return result

    # ------------------------------------------------------------------ #
    #  Signals                                                             #
    # ------------------------------------------------------------------ #
    def add_signal(self, signal: dict):
        with self._lock:
            self.signals.append(signal)
            self._new_signals.append(signal)
            if len(self.signals) > 500:
                self.signals = self.signals[-500:]

    def pop_new_signals(self) -> List[dict]:
        """Drain the new-signals queue (called by UI on each refresh)."""
        with self._lock:
            result = list(self._new_signals)
            self._new_signals.clear()
            return result

    def get_all_signals(self) -> List[dict]:
        with self._lock:
            return list(self.signals)

    # ------------------------------------------------------------------ #
    #  Misc                                                                #
    # ------------------------------------------------------------------ #
    def set_connected(self, status: bool, error: str = ""):
        with self._lock:
            self.connected = status
            self.connection_error = error

    def update_spot_price(self, index: str, price: float):
        with self._lock:
            self.spot_prices[index] = price

    def get_spot_prices(self) -> Dict[str, float]:
        with self._lock:
            return dict(self.spot_prices)


# Singleton used across the whole process
app_state = AppState()
