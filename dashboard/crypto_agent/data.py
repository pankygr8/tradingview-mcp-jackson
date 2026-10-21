"""
data.py — 5-minute BTC / ETH candles.

Sources, in order:
  1. local cache  state/data/<COIN>_5m.csv.gz   (grows every run, kept on the data branch)
  2. Binance Vision public archive (monthly + daily zip files) — long history for the backtest
  3. Hyperliquid info API candleSnapshot — the most recent ~17 days of 5m bars, used live

All timestamps are UTC (tz-aware). Columns: Open High Low Close Volume.
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from . import config

HL_URL = "https://api.hyperliquid.xyz/info"
BV_URL = "https://data.binance.vision/data/spot"
COLS = ["Open", "High", "Low", "Close", "Volume"]


def _frame(rows, ts_unit="ms") -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts"] + COLS)
    df["ts"] = pd.to_datetime(df["ts"], unit=ts_unit, utc=True)
    for c in COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna().set_index("ts").sort_index()


# ── Hyperliquid ──────────────────────────────────────────────────────────────

def hyperliquid(coin: str, days: int = 17, interval: str = "5m") -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out = []
    while start < end:
        r = requests.post(HL_URL, json={"type": "candleSnapshot",
                                        "req": {"coin": coin, "interval": interval,
                                                "startTime": start, "endTime": end}}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out += [[x["t"], x["o"], x["h"], x["l"], x["c"], x["v"]] for x in rows]
        last = rows[-1]["t"]
        if last <= start or len(rows) < 2:
            break
        start = last + 1
    return _frame(out) if out else pd.DataFrame(columns=COLS)


# ── Binance Vision (public archive, no API key) ──────────────────────────────

def _bv_zip(url: str) -> pd.DataFrame | None:
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = pd.read_csv(z.open(z.namelist()[0]), header=None)
        if not str(raw.iloc[0, 0]).isdigit():        # some files carry a header row
            raw = raw.iloc[1:]
        ts = raw[0].astype("int64")
        unit = "us" if ts.iloc[0] > 10**14 else "ms"  # spot switched to microseconds in 2025
        sub = raw[[0, 1, 2, 3, 4, 5]].copy()
        sub[0] = ts
        return _frame(sub.values.tolist(), ts_unit=unit)
    except Exception:
        return None


def binance_vision(symbol: str, months: int, log=print) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    frames = []
    first = (now.replace(day=1) - pd.DateOffset(months=months)).to_pydatetime()
    m = first
    while (m.year, m.month) < (now.year, now.month):
        df = _bv_zip(f"{BV_URL}/monthly/klines/{symbol}/5m/{symbol}-5m-{m:%Y-%m}.zip")
        if df is not None:
            frames.append(df)
        m = (m + pd.DateOffset(months=1)).to_pydatetime()
    d = now.replace(day=1)
    while d.date() < now.date():
        df = _bv_zip(f"{BV_URL}/daily/klines/{symbol}/5m/{symbol}-5m-{d:%Y-%m-%d}.zip")
        if df is not None:
            frames.append(df)
        d += timedelta(days=1)
    if not frames:
        log(f"data: Binance Vision unavailable for {symbol}")
        return pd.DataFrame(columns=COLS)
    out = pd.concat(frames)
    log(f"data: Binance Vision {symbol}: {len(out)} bars {out.index[0]:%Y-%m-%d} → {out.index[-1]:%Y-%m-%d}")
    return out


# ── cache ────────────────────────────────────────────────────────────────────

def _cache_path(coin: str) -> str:
    return os.path.join(config.DATA_DIR, f"{coin}_5m.csv.gz")


def load_cache(coin: str) -> pd.DataFrame:
    p = _cache_path(coin)
    if not os.path.isfile(p):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(p, parse_dates=["ts"], index_col="ts")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def save_cache(coin: str, df: pd.DataFrame, keep_days: int = 400) -> None:
    if df.empty:
        return
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=keep_days)]
    df.to_csv(_cache_path(coin), index_label="ts", float_format="%.6g", compression="gzip")


def _merge(*frames) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=COLS)
    df = pd.concat(frames)
    return df[~df.index.duplicated(keep="last")].sort_index()


def history(coin: str, cfg: dict, months: int | None = None, live_only: bool = False, log=print) -> pd.DataFrame:
    """Cached bars + fresh bars. Hyperliquid wins on overlap (it is the traded venue)."""
    cache = load_cache(coin)
    bv = pd.DataFrame(columns=COLS)
    if not live_only and months:
        have_from = cache.index[0] if not cache.empty else None
        want_from = pd.Timestamp.now(tz="UTC") - pd.DateOffset(months=months)
        if have_from is None or have_from > want_from + pd.Timedelta(days=7):
            bv = binance_vision(cfg["binance_symbol"][coin], months, log=log)
    try:
        hl = hyperliquid(coin, days=17)
        log(f"data: Hyperliquid {coin}: {len(hl)} bars")
    except Exception as e:
        hl = pd.DataFrame(columns=COLS)
        log(f"data: Hyperliquid {coin} failed — {type(e).__name__}: {e}")
    df = _merge(cache, bv, hl)
    save_cache(coin, df)
    return df
