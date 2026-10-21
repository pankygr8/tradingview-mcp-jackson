"""
data.py — Yahoo Finance bars for the commodity futures, cached on disk.

Daily bars: up to `daily_years` years. Hourly bars: Yahoo's ~730-day limit;
the cache keeps every bar ever fetched, so hourly history grows over time.
Prices are delayed ~10–15 min and the continuous front-month series has
small jumps at contract rolls (the MCX contracts roll monthly too).
"""

from __future__ import annotations

import os
import time

import pandas as pd

from . import config

COLS = ["Open", "High", "Low", "Close", "Volume"]


def _path(key: str, interval: str) -> str:
    return os.path.join(config.DATA_DIR, f"{key}_{interval}.csv.gz")


def _load(key, interval) -> pd.DataFrame:
    p = _path(key, interval)
    if not os.path.isfile(p):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(p, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _fetch(ticker: str, interval: str, **kw) -> pd.DataFrame:
    import yfinance as yf
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(interval=interval, auto_adjust=False, **kw)
            if df is not None and not df.empty:
                df = df[[c for c in COLS if c in df.columns]].dropna(subset=["Open", "High", "Low", "Close"])
                df.index = df.index.tz_convert("UTC")
                return df
        except Exception:
            time.sleep(3 * (attempt + 1))
    return pd.DataFrame(columns=COLS)


def bars(key: str, ticker: str, interval: str, cfg: dict, full: bool = False, log=print) -> pd.DataFrame:
    cache = _load(key, interval)
    if interval == "1d":
        kw = {"period": f"{cfg['daily_years']}y"} if (full or cache.empty) else {"period": "1mo"}
    else:
        kw = {"period": f"{cfg['hourly_days']}d"} if (full or cache.empty) else {"period": "30d"}
    fresh = _fetch(ticker, interval, **kw)
    if fresh.empty:
        log(f"data: {ticker} {interval} fetch failed — using cache ({len(cache)} bars)")
    df = pd.concat([cache, fresh]) if not cache.empty else fresh
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if not df.empty:
        df.to_csv(_path(key, interval), compression="gzip", float_format="%.6g")
        log(f"data: {ticker} {interval}: {len(df)} bars {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d %H:%M} UTC")
    return df


def fx_rate(cfg: dict) -> float | None:
    df = _fetch(cfg["fx"], "1d", period="5d")
    return float(df["Close"].iloc[-1]) if not df.empty else None
