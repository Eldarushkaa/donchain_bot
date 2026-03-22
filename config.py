"""
Configuration — loads from .env file via python-dotenv.
All bot and strategy settings live here.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    # ── Bybit API ──────────────────────────────────────────────
    bybit_api_key:    str  = ""
    bybit_api_secret: str  = ""
    bybit_testnet:    bool = False

    # ── Telegram ───────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id:   str = ""

    # ── Bot settings ───────────────────────────────────────────
    symbol:         str   = "ETHUSDT"
    leverage:       int   = 3
    trade_fraction: float = 0.5     # доля свободного баланса на сделку (0.5 = 50%)

    # ── Donchian strategy parameters ──────────────────────────
    n_period:      int   = 42       # breakout channel lookback
    m_period:      int   = 23       # exit channel lookback
    ema200_atr_k:  float = 3.8437   # EMA200 proximity filter coefficient
    vol_ratio_min: float = 1.1976   # min ATR/EMA_ATR for entry


def load_config() -> Config:
    """Build Config from environment variables (loaded from .env)."""
    load_dotenv()
    return Config(
        bybit_api_key    = os.getenv("BYBIT_API_KEY", ""),
        bybit_api_secret = os.getenv("BYBIT_API_SECRET", ""),
        bybit_testnet    = os.getenv("BYBIT_TESTNET", "false").lower() == "true",
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id   = os.getenv("TELEGRAM_CHAT_ID", ""),
        symbol         = os.getenv("SYMBOL", "ETHUSDT"),
        leverage       = int(os.getenv("LEVERAGE", "3")),
        trade_fraction = float(os.getenv("TRADE_FRACTION", "0.5")),
        n_period       = int(os.getenv("N_PERIOD", "42")),
        m_period       = int(os.getenv("M_PERIOD", "23")),
        ema200_atr_k   = float(os.getenv("EMA200_ATR_K", "3.8437")),
        vol_ratio_min  = float(os.getenv("VOL_RATIO_MIN", "1.1976")),
    )
