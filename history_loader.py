"""
history_loader.py
Loads today's 1-minute historical candles from Dhan REST API.
Used to pre-populate charts so they show data immediately on start,
without waiting for the live WebSocket candles to form.
"""
import logging
from datetime import date, datetime
from typing import List

import pytz

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# Instrument type per exchange segment (needed by the REST API)
_INSTRUMENT_TYPE = {
    "NSE_FNO": "OPTIDX",
    "BSE_FNO": "OPTIDX",
    "NSE":     "EQUITY",
    "BSE":     "EQUITY",
}


def _parse_response(resp: dict) -> List[dict]:
    """Convert Dhan intraday_minute_data response into our candle format."""
    candles = []
    if not resp or resp.get("status") != "success":
        return candles

    data = resp.get("data", {})
    opens      = data.get("open",      [])
    highs      = data.get("high",      [])
    lows       = data.get("low",       [])
    closes     = data.get("close",     [])
    timestamps = data.get("timestamp", [])

    # Some versions use start_Time / start_time
    if not timestamps:
        timestamps = data.get("start_Time", data.get("start_time", []))

    for i in range(len(timestamps)):
        try:
            ts_raw = timestamps[i]
            if isinstance(ts_raw, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
                    try:
                        ts = datetime.strptime(ts_raw, fmt)
                        ts = IST.localize(ts)
                        break
                    except ValueError:
                        continue
                else:
                    continue
            else:
                ts = datetime.fromtimestamp(ts_raw / 1000 if ts_raw > 1e10 else ts_raw, IST)

            candles.append({
                "timestamp":    ts.replace(second=0, microsecond=0),
                "open":         float(opens[i]  or 0),
                "high":         float(highs[i]  or 0),
                "low":          float(lows[i]   or 0),
                "close":        float(closes[i] or 0),
                "top_bid_high": 0.0,
                "signal":       False,
                "signal_bid":   None,
                "closed":       True,
            })
        except (IndexError, TypeError, ValueError):
            continue

    return candles


def load_today_candles(
    dhan,
    security_id: str,
    exchange_segment: str,
) -> List[dict]:
    """
    Fetch today's 1-minute candles for one instrument via Dhan REST API.
    Returns list of closed candle dicts (same format as CandleBuilder produces).
    """
    today = date.today().strftime("%Y-%m-%d")
    inst_type = _INSTRUMENT_TYPE.get(exchange_segment, "OPTIDX")

    try:
        resp = dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=inst_type,
            from_date=today,
            to_date=today,
            interval=1,
        )
        # Log the raw response for the first few instruments to help diagnose issues
        if resp and resp.get("status") != "success":
            logger.warning(
                "history_loader: API failure for %s seg=%s: %s",
                security_id, exchange_segment,
                str(resp)[:300],
            )
        candles = _parse_response(resp)
        logger.debug("history_loader: %s → %d candles", security_id, len(candles))
        return candles
    except Exception as exc:
        logger.warning("history_loader: failed for %s: %s", security_id, exc)
        return []


def preload_into_state(dhan, instruments: list, app_state):
    """
    Load historical candles for all instruments and push into app_state.
    Call this once after the feed starts, in a background thread.
    """
    import time

    app_state.history_loaded = False
    app_state.history_error  = None
    logger.info("history_loader: pre-loading candles for %d instruments…", len(instruments))
    loaded = 0
    errors = 0
    for i, inst in enumerate(instruments):
        sid = inst["security_id"]
        seg = inst["exchange_segment"]

        # Skip if we already have candles for this instrument
        if app_state.candles.get(sid):
            continue

        candles = load_today_candles(dhan, sid, seg)
        for c in candles:
            app_state.close_candle(sid, c)
        if candles:
            loaded += len(candles)
        else:
            errors += 1

        # Throttle to avoid REST API rate limiting (100ms per instrument)
        if i % 10 == 9:
            time.sleep(0.5)

    app_state.history_loaded        = True
    app_state.history_candles_count = loaded
    if loaded == 0:
        app_state.history_error = (
            f"REST API returned no candles for any of the {len(instruments)} instruments. "
            "Possible reasons: market not yet open, holiday, or token issue."
        )
        logger.warning("history_loader: loaded 0 candles (%d instruments tried)", errors)
    else:
        logger.info("history_loader: loaded %d total historical candles", loaded)
