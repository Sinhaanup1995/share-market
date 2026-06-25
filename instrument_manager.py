"""
instrument_manager.py
Downloads the Dhan instrument master, finds current weekly expiry,
calculates ATM ± N strikes, and returns subscription-ready dicts.

Dhan master CSV key facts (verified 2026-06):
  - SM_SYMBOL_NAME is NaN for OPTIDX rows
  - Filter by SEM_TRADING_SYMBOL prefix: e.g. "NIFTY-", "BANKNIFTY-", "SENSEX-"
  - SEM_EXPIRY_DATE includes time: "2026-06-30 14:30:00"
  - NSE options → SEM_EXM_EXCH_ID = "NSE"
  - BSE options → SEM_EXM_EXCH_ID = "BSE"
"""
import os
import logging
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
import requests

from config import STRIKE_INTERVALS, OPTIONS_EXCHANGE, ATM_RANGE

logger = logging.getLogger(__name__)

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_FILE = "instruments_cache.csv"

# Exchange to use when filtering the CSV per index
INDEX_EXCHANGE = {
    "NIFTY":     "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX":    "BSE",
}


# ------------------------------------------------------------------ #
#  Master file                                                         #
# ------------------------------------------------------------------ #

def _download_master(force: bool = False) -> pd.DataFrame:
    if not force and os.path.exists(CACHE_FILE):
        mtime      = os.path.getmtime(CACHE_FILE)
        cache_date = datetime.fromtimestamp(mtime).date()
        if cache_date == date.today():
            logger.info("instrument_manager: using today's cached master")
            return pd.read_csv(CACHE_FILE, low_memory=False)

    logger.info("instrument_manager: downloading master from Dhan …")
    resp = requests.get(MASTER_URL, timeout=30)
    resp.raise_for_status()
    with open(CACHE_FILE, "wb") as fh:
        fh.write(resp.content)
    return pd.read_csv(CACHE_FILE, low_memory=False)


# ------------------------------------------------------------------ #
#  Expiry: pick nearest upcoming date from the CSV itself              #
# ------------------------------------------------------------------ #

def get_nearest_expiry(df: pd.DataFrame, index: str, reference: Optional[date] = None) -> Optional[date]:
    """Return the nearest expiry >= today from actual CSV data for this index."""
    today    = reference or date.today()
    exchange = INDEX_EXCHANGE[index]
    prefix   = f"{index}-"

    mask = (
        (df["SEM_INSTRUMENT_NAME"] == "OPTIDX") &
        (df["SEM_EXM_EXCH_ID"] == exchange) &
        (df["SEM_TRADING_SYMBOL"].str.startswith(prefix))
    )
    subset = df[mask]
    if subset.empty:
        logger.warning("instrument_manager: no OPTIDX rows found for %s", index)
        return None

    expiry_dates = pd.to_datetime(subset["SEM_EXPIRY_DATE"], errors="coerce").dt.date.dropna().unique()
    upcoming     = sorted(d for d in expiry_dates if d >= today)
    if not upcoming:
        logger.warning("instrument_manager: no upcoming expiry found for %s", index)
        return None

    chosen = upcoming[0]
    logger.info("instrument_manager: %s nearest expiry = %s", index, chosen)
    return chosen


# ------------------------------------------------------------------ #
#  ATM                                                                 #
# ------------------------------------------------------------------ #

def get_atm_strike(spot: float, index: str) -> int:
    step = STRIKE_INTERVALS[index]
    return int(round(spot / step) * step)


# ------------------------------------------------------------------ #
#  Core: get instruments for one index                                 #
# ------------------------------------------------------------------ #

def get_option_instruments(
    df: pd.DataFrame,
    index: str,
    expiry: date,
    atm: int,
    atm_range: int = ATM_RANGE,
) -> List[dict]:
    step     = STRIKE_INTERVALS[index]
    strikes  = {atm + i * step for i in range(-atm_range, atm_range + 1)}
    exchange = INDEX_EXCHANGE[index]
    prefix   = f"{index}-"

    # ---- filter ----
    df2 = df[
        (df["SEM_INSTRUMENT_NAME"] == "OPTIDX") &
        (df["SEM_EXM_EXCH_ID"] == exchange) &
        (df["SEM_TRADING_SYMBOL"].str.startswith(prefix))
    ].copy()

    if df2.empty:
        logger.warning("instrument_manager: no OPTIDX rows for %s", index)
        return []

    # Expiry filter (CSV has "2026-06-30 14:30:00" format)
    df2["_expiry_date"] = pd.to_datetime(df2["SEM_EXPIRY_DATE"], errors="coerce").dt.date
    df2 = df2[df2["_expiry_date"] == expiry]

    if df2.empty:
        logger.warning("instrument_manager: no rows for %s on expiry %s", index, expiry)
        return []

    # Strike filter
    df2["SEM_STRIKE_PRICE"] = pd.to_numeric(df2["SEM_STRIKE_PRICE"], errors="coerce")
    df2 = df2[df2["SEM_STRIKE_PRICE"].isin(strikes)]

    if df2.empty:
        logger.warning("instrument_manager: no matching strikes for %s atm=%d strikes=%s", index, atm, sorted(strikes))
        return []

    seg_code = OPTIONS_EXCHANGE[index]   # e.g. "NSE_FNO"
    result   = []
    for _, row in df2.iterrows():
        sid    = str(row["SEM_SMST_SECURITY_ID"]).strip()
        sym    = str(row["SEM_TRADING_SYMBOL"]).strip()
        strike = float(row["SEM_STRIKE_PRICE"])
        opt    = str(row["SEM_OPTION_TYPE"]).strip().upper()

        if not sid or opt not in ("CE", "PE"):
            continue

        result.append({
            "security_id":      sid,
            "trading_symbol":   sym,
            "strike_price":     strike,
            "option_type":      opt,
            "index":            index,
            "expiry_date":      expiry,
            "exchange_segment": seg_code,
            "atm_distance":     int((strike - atm) / step),
        })

    logger.info(
        "instrument_manager: %s → %d instruments (expiry=%s atm=%d)",
        index, len(result), expiry, atm,
    )
    return result


# ------------------------------------------------------------------ #
#  Public facade                                                       #
# ------------------------------------------------------------------ #

class InstrumentManager:
    def __init__(self):
        self._df: Optional[pd.DataFrame] = None

    def load_master(self, force: bool = False):
        self._df = _download_master(force=force)

    def build_subscriptions(
        self,
        spot_prices: Dict[str, float],
        atm_range: int = ATM_RANGE,
    ) -> List[dict]:
        """
        Given current spot prices, return a flat list of instrument dicts
        covering ATM ± atm_range for NIFTY, BANKNIFTY, SENSEX.
        """
        if self._df is None:
            self.load_master()

        all_instruments: List[dict] = []
        for index, spot in spot_prices.items():
            if spot <= 0:
                logger.warning("instrument_manager: skipping %s (spot=%.2f)", index, spot)
                continue
            expiry = get_nearest_expiry(self._df, index)
            if expiry is None:
                logger.error("instrument_manager: could not determine expiry for %s", index)
                continue
            atm   = get_atm_strike(spot, index)
            insts = get_option_instruments(self._df, index, expiry, atm, atm_range)
            all_instruments.extend(insts)

        return all_instruments
