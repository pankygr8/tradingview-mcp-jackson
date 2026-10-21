"""
data_source.py — Pluggable market-data backend.

To add a new broker (Dhan, DeltaExchange, Zerodha, etc.) create a subclass
of DataSource, implement the three abstract methods, and register it in
DATA_SOURCES at the bottom.  Nothing else needs to change.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from functools import lru_cache
from threading import Lock
from typing import Any

import pandas as pd
import yfinance as yf


# ── Shared timeframe helpers ──────────────────────────────────────────────────

# Canonical TF labels used by the frontend → yfinance interval string
_YF_INTERVAL: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "60m",
    "4h":  "60m",   # fetched as 1h, resampled to 4h below
    "1d":  "1d",
    "1w":  "1wk",
}

# How far back to fetch for each TF (yfinance hard limits)
_YF_PERIOD: dict[str, str] = {
    "1m":  "7d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "1h":  "730d",
    "4h":  "730d",
    "1d":  "5y",
    "1w":  "10y",
}


# ── Simple TTL cache ──────────────────────────────────────────────────────────

class _TTLCache:
    """Thread-safe in-memory cache with per-entry TTL."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() < entry[0]:
                return entry[1]
            return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._store[key] = (time.time() + ttl, value)


_cache = _TTLCache()


# ── Abstract interface ────────────────────────────────────────────────────────

class DataSource(ABC):
    """
    Interface every data source must satisfy.

    All methods should raise on unrecoverable error; the Flask layer
    converts exceptions to JSON error responses.
    """

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, count: int = 150) -> list[dict]:
        """
        Return up to *count* OHLCV bars, oldest first.

        Each dict must have:
            time   – Unix timestamp (seconds, UTC)
            open   – float
            high   – float
            low    – float
            close  – float
            volume – float | int
        """

    @abstractmethod
    def get_quote(self, symbol: str) -> dict:
        """
        Return a single latest-price snapshot.

        Must include: symbol, price, change, change_pct, time (UTC unix seconds).
        """

    @abstractmethod
    def get_symbols(self) -> list[str]:
        """Return the list of symbols this source supports."""

    @abstractmethod
    def get_timeframes(self) -> list[str]:
        """Return supported TF labels (e.g. ['1m','5m','1h','1d'])."""


# ── yfinance implementation ───────────────────────────────────────────────────

class YFinanceSource(DataSource):
    """
    Indian equities (NSE/BSE) and global indices via yfinance.

    Limitations:
    • Intraday data has a 15-20 min delay and yfinance rate limits apply.
    • 1-min data is only available for the past 7 days.
    • 4h is synthesised by resampling 1h bars.
    """

    SYMBOLS: list[str] = [
        # Nifty 50 heavyweights
        "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
        "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "BAJFINANCE.NS",
        "LT.NS", "WIPRO.NS", "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS",
        "TITAN.NS", "SUNPHARMA.NS", "ULTRACEMCO.NS", "TECHM.NS", "NESTLEIND.NS",
        "HCLTECH.NS", "POWERGRID.NS", "NTPC.NS", "COALINDIA.NS", "ONGC.NS",
        "ADANIENT.NS", "ADANIPORTS.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
        "JSWSTEEL.NS", "BAJAJFINSV.NS", "BPCL.NS", "CIPLA.NS", "DRREDDY.NS",
        "DIVISLAB.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "INDUSINDBK.NS",
        "M&M.NS", "EICHERMOT.NS", "GRASIM.NS", "APOLLOHOSP.NS", "BRITANNIA.NS",
        # Indices
        "^NSEI",    # Nifty 50
        "^BSESN",   # Sensex
        "^NSEBANK", # Bank Nifty
    ]

    TIMEFRAMES: list[str] = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

    def get_symbols(self) -> list[str]:
        return self.SYMBOLS

    def get_timeframes(self) -> list[str]:
        return self.TIMEFRAMES

    def get_ohlcv(self, symbol: str, timeframe: str, count: int = 150) -> list[dict]:
        cache_key = f"ohlcv:{symbol}:{timeframe}:{count}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        interval = _YF_INTERVAL.get(timeframe, "1d")
        period   = _YF_PERIOD.get(timeframe, "1y")

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)

        if df.empty:
            return []

        # Resample 1h → 4h when needed
        if timeframe == "4h":
            df = (
                df.resample("4h", closed="left", label="left")
                .agg({"Open": "first", "High": "max", "Low": "min",
                      "Close": "last", "Volume": "sum"})
                .dropna(subset=["Open"])
            )

        df = df.tail(count)

        rows = []
        for ts, row in df.iterrows():
            # ts may be tz-aware; convert to UTC unix seconds
            if hasattr(ts, "timestamp"):
                unix = int(ts.timestamp())
            else:
                unix = int(pd.Timestamp(ts).timestamp())
            rows.append({
                "time":   unix,
                "open":   round(float(row["Open"]),   4),
                "high":   round(float(row["High"]),   4),
                "low":    round(float(row["Low"]),    4),
                "close":  round(float(row["Close"]),  4),
                "volume": int(row.get("Volume", 0)),
            })

        ttl = 60 if timeframe in ("1m", "5m") else 300
        _cache.set(cache_key, rows, ttl)
        return rows

    def get_quote(self, symbol: str) -> dict:
        cache_key = f"quote:{symbol}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        ticker = yf.Ticker(symbol)
        now = int(datetime.now(timezone.utc).timestamp())

        # Try fast_info first (lighter call)
        try:
            fi = ticker.fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close or price)
            chg   = price - prev
            chg_p = (chg / prev * 100) if prev else 0.0
            result = {
                "symbol":     symbol,
                "price":      round(price, 4),
                "change":     round(chg,   4),
                "change_pct": round(chg_p, 4),
                "time":       now,
            }
            _cache.set(cache_key, result, 5)
            return result
        except Exception:
            pass

        # Fallback: last two daily bars
        try:
            df = ticker.history(period="5d", interval="1d", auto_adjust=True)
            if len(df) >= 2:
                price = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2])
            elif len(df) == 1:
                price = float(df["Close"].iloc[-1])
                prev  = float(df["Open"].iloc[-1])
            else:
                return {"symbol": symbol, "price": 0, "change": 0, "change_pct": 0, "time": now}

            chg   = price - prev
            chg_p = (chg / prev * 100) if prev else 0.0
            result = {
                "symbol":     symbol,
                "price":      round(price, 4),
                "change":     round(chg,   4),
                "change_pct": round(chg_p, 4),
                "time":       now,
            }
            _cache.set(cache_key, result, 10)
            return result
        except Exception as exc:
            return {"symbol": symbol, "price": 0, "change": 0, "change_pct": 0,
                    "time": now, "error": str(exc)}


# ── Stub classes for future brokers ──────────────────────────────────────────
#
# Uncomment and fill in the implementation, then add to DATA_SOURCES below.
#
# class DhanSource(DataSource):
#     """Dhan broker — https://api.dhan.co"""
#     SYMBOLS = [...]
#     TIMEFRAMES = [...]
#
#     def get_ohlcv(self, symbol, timeframe, count=150):
#         # GET https://api.dhan.co/charts/historical?...
#         ...
#
#     def get_quote(self, symbol):
#         # GET https://api.dhan.co/quotes?symbol=...
#         ...
#
#     def get_symbols(self):
#         return self.SYMBOLS
#
#     def get_timeframes(self):
#         return self.TIMEFRAMES
#
#
# class DeltaExchangeSource(DataSource):
#     """Delta Exchange — https://api.delta.exchange"""
#     ...
#
#
# class ZerodhaSource(DataSource):
#     """Zerodha Kite — https://kite.zerodha.com/api"""
#     ...


# ── Registry ─────────────────────────────────────────────────────────────────

DATA_SOURCES: dict[str, DataSource] = {
    "yfinance": YFinanceSource(),
    # "dhan":    DhanSource(),
    # "delta":   DeltaExchangeSource(),
    # "zerodha": ZerodhaSource(),
}


def get_source(name: str = "yfinance") -> DataSource:
    """Return a registered DataSource by name, defaulting to yfinance."""
    return DATA_SOURCES.get(name) or DATA_SOURCES["yfinance"]
