"""
data.py — Yahoo Finance prices for the Nifty 200, cached in state/data/.

  daily.csv.gz   full daily history (needed for all-time highs), refreshed once a day
  m15.csv.gz     15-minute bars, merged run after run (Yahoo only serves 60 days,
                 so this file grows into a longer intraday history over time)
  h1.csv.gz      1-hour bars, last ~2 years (backtest only)

Prices are split/dividend adjusted. If an incremental daily refresh disagrees
with the cached history by more than ADJ_TOL (0.1 % — any dividend, split or
bonus re-adjustment), that symbol is re-downloaded in full, so old and new bars
always share one adjustment basis.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

from . import config

IST = "Asia/Kolkata"
COLS = ["Open", "High", "Low", "Close", "Volume"]
ADJ_TOL = 0.001          # relative close mismatch that counts as a re-adjustment


def _yf():
    import yfinance as yf
    return yf


def download(symbols: list[str], log=print, chunk: int = 50, **kw) -> dict[str, pd.DataFrame]:
    """Batch download; returns {symbol: OHLCV frame}. Missing symbols are skipped."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        for attempt in range(3):
            try:
                df = _yf().download(part, group_by="ticker", auto_adjust=True, progress=False,
                                    threads=True, **kw)
                break
            except Exception as e:                       # rate limit / network blip
                log(f"data: download error ({type(e).__name__}: {e}) — retry {attempt + 1}")
                time.sleep(5 * (attempt + 1))
        else:
            continue
        if df is None or df.empty:
            continue
        for s in part:
            try:
                d = df[s] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            if not all(c in d.columns for c in COLS):
                continue
            d = d[COLS].dropna(subset=["Open", "High", "Low", "Close"])
            d = d[(d[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
            if not d.empty:
                out[s] = d.astype(float)
    return out


# ── cache files (long format: sym, ts, OHLCV) ────────────────────────────────
def _path(name):
    return os.path.join(config.DATA_DIR, f"{name}.csv.gz")


def _save(name, frames: dict[str, pd.DataFrame]):
    parts = []
    for s, d in frames.items():
        x = d[COLS].copy()
        x[["Open", "High", "Low", "Close"]] = x[["Open", "High", "Low", "Close"]].round(2)
        x["Volume"] = x["Volume"].fillna(0).round().astype("int64")
        x.insert(0, "sym", s)
        x.index.name = "ts"
        parts.append(x.reset_index())
    if parts:
        pd.concat(parts).to_csv(_path(name), index=False, compression="gzip")


def _load(name, intraday: bool) -> dict[str, pd.DataFrame]:
    p = _path(name)
    if not os.path.isfile(p):
        return {}
    df = pd.read_csv(p)
    if intraday:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
    else:
        df["ts"] = pd.to_datetime(df["ts"].astype(str).str[:10])
    return {s: g.set_index("ts")[COLS].sort_index() for s, g in df.groupby("sym")}


def _naive_dates(d: pd.DataFrame) -> pd.DataFrame:
    idx = d.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    d = d.copy()
    d.index = pd.DatetimeIndex(idx).normalize()
    return d[~d.index.duplicated(keep="last")]


# Bump when the daily cache must be rebuilt from scratch. 2 = Nov 2026: caches built
# before then can hold old unadjusted history spliced to re-adjusted recent bars.
DAILY_BASIS = 2


def _basis_path():
    return os.path.join(config.DATA_DIR, "daily_basis.txt")


def _basis() -> int:
    try:
        with open(_basis_path()) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def daily(symbols: list[str], refresh: bool = True, log=print) -> dict[str, pd.DataFrame]:
    frames = _load("daily", intraday=False)
    rebuilt = False
    if refresh and frames and _basis() < DAILY_BASIS:
        log(f"data: one-time full re-download of {len(frames)} symbols (daily cache basis {_basis()} → {DAILY_BASIS})")
        got = download(list(frames), log=log, period="max", interval="1d")
        for s, d in got.items():
            frames[s] = _naive_dates(d)
        rebuilt = set(frames) <= set(got)       # only mark done if every symbol came back
    elif not frames:
        rebuilt = True                          # a fresh cache is built on one basis anyway
    missing = [s for s in symbols if s not in frames]
    if missing:
        log(f"data: full daily history for {len(missing)} symbols")
        for s, d in download(missing, log=log, period="max", interval="1d").items():
            frames[s] = _naive_dates(d)
    if refresh and frames:
        fresh = download(symbols, log=log, period="1mo", interval="1d")
        redo = []
        for s, d in fresh.items():
            d = _naive_dates(d)
            old = frames.get(s)
            if old is None or old.empty:
                frames[s] = d
                continue
            common = old.index.intersection(d.index)[:-1]          # last bar may be forming
            # Prices are auto-adjusted, so a dividend re-scales every bar before its ex-date.
            # Splicing re-adjusted recent bars onto old unadjusted history leaves a step in the
            # series (MAZDOCK, Nov 2026: last month x0.9971, ATH untouched → a past signal
            # vanished). Any re-adjustment — dividend, split or bonus — refetches everything.
            if len(common) and (abs(old.loc[common, "Close"] / d.loc[common, "Close"] - 1) > ADJ_TOL).any():
                redo.append(s)
                continue
            frames[s] = pd.concat([old[old.index < d.index[0]], d])
        if redo:
            log(f"data: price adjustment detected, re-downloading full history for {redo}")
            for s, d in download(redo, log=log, period="max", interval="1d").items():
                frames[s] = _naive_dates(d)
    frames = {s: d for s, d in frames.items() if s in symbols or s.startswith("^")}
    _save("daily", frames)
    if rebuilt and frames:
        with open(_basis_path(), "w") as f:
            f.write(str(DAILY_BASIS))
    return frames


def intraday(symbols: list[str], interval: str, period: str, cache: str | None,
             keep_days: int = 400, log=print) -> dict[str, pd.DataFrame]:
    frames = _load(cache, intraday=True) if cache else {}
    fresh = download(symbols, log=log, period=period, interval=interval)
    for s, d in fresh.items():
        d = d.copy()
        d.index = (d.index.tz_localize("UTC") if d.index.tz is None else d.index).tz_convert(IST)
        old = frames.get(s)
        frames[s] = d if old is None else pd.concat([old[old.index < d.index[0]], d])
    cut = pd.Timestamp.now(tz=IST) - pd.Timedelta(days=keep_days)
    frames = {s: d[d.index >= cut] for s, d in frames.items() if s in symbols}
    if cache:
        _save(cache, frames)
    return frames


def bar_minutes(interval: str) -> int:
    return {"15m": 15, "5m": 5, "30m": 30, "60m": 60, "1h": 60}[interval]


def complete_bars(d: pd.DataFrame, minutes: int, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Drop the bar that is still forming."""
    now = now or pd.Timestamp.now(tz=IST)
    return d[d.index + pd.Timedelta(minutes=minutes) <= now]


def atr(d: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = d["Close"].shift()
    tr = np.maximum(d["High"] - d["Low"], np.maximum((d["High"] - pc).abs(), (d["Low"] - pc).abs()))
    return tr.rolling(n).mean()
