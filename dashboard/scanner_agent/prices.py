"""
prices.py — price history for validation and replay, with a small disk cache.

Daily bars: full history per symbol, cached once per calendar day as a pickle
in scanner_agent/cache/daily/. The replay and the validator share it, so a
symbol is downloaded at most once a day however many signals reference it.

Intraday bars: 5-minute, last ~60 days (yfinance hard limit), memory-cached
per process only.
"""

from __future__ import annotations

import os
import time
from datetime import date

import pandas as pd

from . import config

_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _yf():
    import yfinance as yf
    return yf


def _clean(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = df.copy()
    for c in _COLS:
        if c not in df.columns:
            if c == "Volume":
                df[c] = 0
            else:
                return None
    df = df[_COLS].dropna(subset=["Open", "High", "Low", "Close"])
    return df if not df.empty else None


class PriceCache:
    def __init__(self, max_age_hours: float = 12.0, offline: bool = False):
        self.max_age = max_age_hours * 3600
        self.offline = offline
        self._daily: dict[str, pd.DataFrame | None] = {}
        self._intra: dict[str, pd.DataFrame | None] = {}
        self.dir = os.path.join(config.CACHE_DIR, "daily")
        os.makedirs(self.dir, exist_ok=True)

    # ── daily ────────────────────────────────────────────────────────────────
    def _path(self, symbol: str) -> str:
        safe = symbol.replace("^", "_").replace("/", "_").replace("=", "_")
        return os.path.join(self.dir, f"{safe}.pkl")

    def daily(self, symbol: str) -> pd.DataFrame | None:
        """Full daily history, index = tz-naive midnight dates."""
        if symbol in self._daily:
            return self._daily[symbol]
        path = self._path(symbol)
        df = None
        if os.path.isfile(path) and (self.offline or time.time() - os.path.getmtime(path) < self.max_age):
            try:
                df = pd.read_pickle(path)
            except Exception:
                df = None
        if df is None and not self.offline:
            df = None
            for attempt in range(3):
                try:
                    raw = _yf().Ticker(symbol).history(period="max", interval="1d", auto_adjust=True)
                    df = _clean(raw)
                    break
                except Exception:
                    time.sleep(2 * (attempt + 1))
            if df is not None:
                idx = df.index
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                df.index = pd.DatetimeIndex(idx).normalize()
                df = df[~df.index.duplicated(keep="last")]
                try:
                    df.to_pickle(path)
                except Exception:
                    pass
        self._daily[symbol] = df
        return df

    # ── intraday ─────────────────────────────────────────────────────────────
    def intraday(self, symbol: str) -> pd.DataFrame | None:
        """5-minute bars for the last ~60 days, index tz-aware Asia/Kolkata."""
        if symbol in self._intra:
            return self._intra[symbol]
        df = None
        if not self.offline:
            for attempt in range(3):
                try:
                    raw = _yf().Ticker(symbol).history(period="60d", interval="5m", auto_adjust=False)
                    df = _clean(raw)
                    break
                except Exception:
                    time.sleep(2 * (attempt + 1))
            if df is not None:
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                df.index = df.index.tz_convert("Asia/Kolkata")
        self._intra[symbol] = df
        return df

    def prefetch_daily(self, symbols, workers: int = 8) -> None:
        from concurrent.futures import ThreadPoolExecutor
        todo = [s for s in symbols if s not in self._daily]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self.daily, todo))


def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    if df is None or len(df) < period + 1:
        return None
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    v = float(tr.tail(period).mean())
    return v if v > 0 else None


def today_ist() -> date:
    from .adapters import ist_now
    return ist_now().date()
