import os
from dotenv import load_dotenv

load_dotenv()

DHAN_CLIENT_ID: str = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN: str = os.environ.get("DHAN_ACCESS_TOKEN", "")

# Strike step intervals per index
STRIKE_INTERVALS: dict = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "SENSEX": 100,
}

# How many strikes above and below ATM to subscribe
ATM_RANGE: int = 5

# Dhan security IDs for spot index feed (IDX_I segment)
INDEX_SECURITY_IDS: dict = {
    "NIFTY":     {"exchange_segment": "IDX_I", "security_id": "13"},
    "BANKNIFTY": {"exchange_segment": "IDX_I", "security_id": "25"},
    "SENSEX":    {"exchange_segment": "IDX_I", "security_id": "51"},
}

# Option exchange segment per index
OPTIONS_EXCHANGE: dict = {
    "NIFTY":     "NSE_FNO",
    "BANKNIFTY": "NSE_FNO",
    "SENSEX":    "BSE_FNO",
}

# Weekly expiry weekday (0=Mon … 6=Sun)
EXPIRY_WEEKDAY: dict = {
    "NIFTY":     3,   # Thursday
    "BANKNIFTY": 2,   # Wednesday
    "SENSEX":    4,   # Friday
}

# Fallback spot prices if REST API is unavailable
DEFAULT_SPOT: dict = {
    "NIFTY":     24500,
    "BANKNIFTY": 54000,
    "SENSEX":    81000,
}

MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"
