"""
signal_engine.py — Python reimplementation of swing-fib-angle-alert.pine v3.2
                   "Swing Fib Box + Angle [v3.2]"

Supports two timeframes:
  "1d"  — Daily   (swing / positional trading)
  "15m" — 15-min  (intraday trading)

Signal logic matches Pine Script v3.2 exactly:
  • Pivot highs/lows  → ta.pivothigh / ta.pivotlow equivalent
  • swingMode: "Largest Range" (biggest H-L swing) or "Most Recent" (latest pivot pair)
  • Gann Octave fib levels: 0, 1/8, 1/4, 1/2, 3/4, 7/8, 1/1
  • Clone geometry (visual only, not used in signal logic):
      Upper clone: main box shifted UP by fibRng  (0 of clone = 1/1 of main)
      Lower clone: main box shifted DOWN by fibRng (1/1 of clone = 0 of main)
  • bosMode:
      "Simple" → fib bounce only (cross above 1/8 or below 7/8)
      "Strict" / "Both" → fib bounce AND Gann angle confirmation (Pine default)
  • Angle lines:
      Up angle  — from sH  @ swEnd to f0875 @ projEnd (red in Pine)
      Down angle — from sL  @ swEnd to f0125 @ projEnd (green in Pine)
  • De-duplication: no same-direction signal within min_bar_gap bars
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import math

import numpy as np
import pandas as pd
import yfinance as yf

from screener import ALL_SYMBOLS, UNIVERSES

# ── Per-timeframe configuration ───────────────────────────────────────────────

TF_CONFIG: dict[str, dict] = {
    "1d": {
        "yf_period":    "2y",
        "yf_interval":  "1d",
        "piv_len":      12,
        "lb_bars":      200,
        "min_rng_pct":  1.0,
        "min_bar_gap":  5,
        "recency_bars": 5,       # signal within 5 trading days = active
        "label":        "Daily",
    },
    "15m": {
        "yf_period":    "60d",
        "yf_interval":  "15m",
        "piv_len":      12,      # matches Pine Script default (12 × 15 min = 3 h each side)
        "lb_bars":      200,     # matches Pine Script default — allows ~8 trading day swings
        "min_rng_pct":  0.3,     # smaller threshold for intraday
        "min_bar_gap":  5,       # 5 × 15 min = 75 min minimum gap between signals
        "recency_bars": 75,      # ~3 trading days of 15m bars = still-relevant signal
        "label":        "15-min Intraday",
    },
}

MAX_WORKERS = 10
CACHE_TTL   = {
    "1d":  3 * 3600,   # 3-hour cache for daily
    "15m": 15 * 60,    # 15-minute cache for intraday
}

# ── In-memory job store ───────────────────────────────────────────────────────

_jobs:  dict[str, dict[str, Any]] = {}
_cache: dict[str, dict[str, Any]] = {}   # key = f"{universe}_{timeframe}"
_lock   = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# Core signal computation (timeframe-agnostic)
# ═══════════════════════════════════════════════════════════════════════════════

def _pivot_high(high: np.ndarray, length: int) -> np.ndarray:
    n  = len(high)
    ph = np.full(n, np.nan)
    for i in range(length, n - length):
        w = high[i - length: i + length + 1]
        if high[i] == w.max() and np.sum(w == high[i]) == 1:
            ph[i] = high[i]
    return ph


def _pivot_low(low: np.ndarray, length: int) -> np.ndarray:
    n  = len(low)
    pl = np.full(n, np.nan)
    for i in range(length, n - length):
        w = low[i - length: i + length + 1]
        if low[i] == w.min() and np.sum(w == low[i]) == 1:
            pl[i] = low[i]
    return pl


def compute_signals(
    df,
    cfg: dict,
    bos_mode:   str = "Both",          # "Simple" | "Strict" | "Both"
    swing_mode: str = "Largest Range", # "Largest Range" | "Most Recent"
    lb_bars:    "int | None" = None,   # overrides cfg["lb_bars"] when provided
) -> dict:
    """
    Run Swing Fib Angle Alert on an OHLCV DataFrame using config from TF_CONFIG.
    Matches Pine Script v3.2 exactly.

    bos_mode:
        "Simple"        — fib bounce only (no angle confirmation)
        "Strict"/"Both" — fib bounce AND Gann angle confirmation  [Pine default]

    swing_mode:
        "Largest Range" — biggest H-L swing within lb_bars  [Pine default]
        "Most Recent"   — latest confirmed pivot pair

    lb_bars:
        When None (default), uses cfg["lb_bars"] (200 for 1d, 200 for 15m).
        Pass an int to override — e.g. 50 for fast-trending stocks.

    Returns dict: signals, recent_signals, fib_levels, current_signal.
    """
    piv_len      = cfg["piv_len"]
    lb_bars      = lb_bars if lb_bars is not None else cfg["lb_bars"]
    min_rng_pct  = cfg["min_rng_pct"]
    min_bar_gap  = cfg["min_bar_gap"]
    recency_bars = cfg["recency_bars"]

    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n     = len(df)

    EMPTY = {"signals": [], "recent_signals": [], "fib_levels": None, "current_signal": None}
    if n < piv_len * 2 + 10:
        return EMPTY

    # ── Pivot arrays (MAX_PV = 30) ────────────────────────────────────────────
    ph_arr = _pivot_high(high, piv_len)
    pl_arr = _pivot_low(low,  piv_len)

    ph_vals: list[float] = []; ph_bars: list[int] = []
    pl_vals: list[float] = []; pl_bars: list[int] = []
    MAX_PV = 30

    for i in range(n):
        if not np.isnan(ph_arr[i]):
            ph_vals.append(ph_arr[i]); ph_bars.append(i)
            if len(ph_vals) > MAX_PV: ph_vals.pop(0); ph_bars.pop(0)
        if not np.isnan(pl_arr[i]):
            pl_vals.append(pl_arr[i]); pl_bars.append(i)
            if len(pl_vals) > MAX_PV: pl_vals.pop(0); pl_bars.pop(0)

    if not ph_vals or not pl_vals:
        return EMPTY

    nH, nL  = len(ph_vals), len(pl_vals)
    cur_bar = n - 1
    eff_min = close[-1] * min_rng_pct / 100
    sH = sHB = sL = sLB = None

    # ── Swing selection — matches Pine Script swingMode input ─────────────────
    if swing_mode == "Most Recent":
        # Pine "Most Recent": use the single most recent pivot high and low
        sH  = ph_vals[-1]
        sHB = ph_bars[-1]
        sL  = pl_vals[-1]
        sLB = pl_bars[-1]
        # Reject if range is below minimum threshold
        if sH - sL < eff_min:
            return EMPTY
    else:
        # Pine "Largest Range": search up to 15 recent pivot pairs, pick biggest H-L
        best_rng = eff_min
        cap = 15
        for i in range(nH - 1, max(-1, nH - cap - 1), -1):
            for j in range(nL - 1, max(-1, nL - cap - 1), -1):
                h, hb = ph_vals[i], ph_bars[i]
                l, lb = pl_vals[j], pl_bars[j]
                spn   = abs(hb - lb)
                if spn >= piv_len and spn <= lb_bars and cur_bar - min(hb, lb) <= lb_bars:
                    rng = h - l
                    if rng > best_rng:
                        best_rng, sH, sHB, sL, sLB = rng, h, hb, l, lb

    if sH is None:
        return EMPTY

    # ── Gann Octave fib levels ────────────────────────────────────────────────
    fib_rng = sH - sL
    f0    = sL
    f0125 = sL + 0.125 * fib_rng
    f025  = sL + 0.250 * fib_rng
    f050  = sL + 0.500 * fib_rng
    f075  = sL + 0.750 * fib_rng
    f0875 = sL + 0.875 * fib_rng
    f1    = sH

    sw_start = min(sHB, sLB)
    sw_end   = max(sHB, sLB)
    sw_dur   = max(sw_end - sw_start, 1)
    proj_end = sw_end + sw_dur

    def ang_up(b): return sH  + (f0875 - sH)  * (b - sw_end) / sw_dur
    def ang_dn(b): return sL  + (f0125 - sL)  * (b - sw_end) / sw_dur

    # ── Signal detection ──────────────────────────────────────────────────────
    signals: list[dict] = []
    last_was_long  = False; last_was_short = False
    last_long_bar  = 0;     last_short_bar = 0

    for i in range(1, n):
        active = sw_start <= i <= proj_end + sw_dur * 2

        sell_fib = active and close[i] < f0875 and close[i-1] >= f0875
        buy_fib  = active and close[i] > f0125 and close[i-1] <= f0125
        confirmed_sell = close[i] < ang_up(i)
        confirmed_buy  = close[i] > ang_dn(i)

        # bosMode: "Simple" = fib bounce only; "Strict"/"Both" = fib + angle
        # Matches Pine: longSig = bosMode=="Simple" ? buyFib : (buyFib and confirmedBuy)
        if bos_mode == "Simple":
            long_sig  = buy_fib
            short_sig = sell_fib
        else:
            long_sig  = buy_fib  and confirmed_buy
            short_sig = sell_fib and confirmed_sell

        long_fire  = long_sig  and not last_was_long  and (i - last_long_bar  >= min_bar_gap)
        short_fire = short_sig and not last_was_short and (i - last_short_bar >= min_bar_gap)

        if long_fire:
            # ── 1-bar close confirmation ──────────────────────────────────────
            # numpy comparisons return numpy.bool_ which is NOT JSON-serialisable.
            # Explicitly cast every boolean field to Python bool.
            if i + 1 < n:
                next_confirmed: bool | None = bool(close[i + 1] > f0125)
            else:
                next_confirmed = None   # live bar — next candle not yet closed

            bullish_bar = bool(float(close[i]) > float(df["open"].iloc[i]))
            is_valid    = bool(next_confirmed and bullish_bar) if next_confirmed is not None else None

            signals.append({
                "bar": i, "date": _bar_label(df, i),
                "type": "BUY",  "price": round(float(close[i]), 2),
                "f0125": round(float(f0125), 2), "f0875": round(float(f0875), 2),
                "swing_high": round(float(sH), 2), "swing_low": round(float(sL), 2),
                "bars_ago": int(cur_bar - i),
                "next_close_ok": next_confirmed,
                "signal_bar_ok": bullish_bar,
                "valid":         is_valid,
            })
            last_was_long = True; last_was_short = False; last_long_bar = i

        elif short_fire:
            if i + 1 < n:
                next_confirmed = bool(close[i + 1] < f0875)
            else:
                next_confirmed = None

            bearish_bar = bool(float(close[i]) < float(df["open"].iloc[i]))
            is_valid    = bool(next_confirmed and bearish_bar) if next_confirmed is not None else None

            signals.append({
                "bar": i, "date": _bar_label(df, i),
                "type": "SELL", "price": round(float(close[i]), 2),
                "f0125": round(float(f0125), 2), "f0875": round(float(f0875), 2),
                "swing_high": round(float(sH), 2), "swing_low": round(float(sL), 2),
                "bars_ago": int(cur_bar - i),
                "next_close_ok": next_confirmed,
                "signal_bar_ok": bearish_bar,
                "valid":         is_valid,
            })
            last_was_short = True; last_was_long = False; last_short_bar = i

        elif not long_sig and not short_sig:
            last_was_long = False; last_was_short = False

    recent  = [s for s in signals if s["bars_ago"] <= recency_bars]
    current = signals[-1] if signals and signals[-1]["bars_ago"] <= recency_bars else None

    return {
        "signals":        signals[-20:],
        "recent_signals": recent,
        "fib_levels": {
            "swing_high":     round(float(sH), 2), "swing_low":     round(float(sL), 2),
            "swing_high_bar": int(sHB),            "swing_low_bar": int(sLB),
            "f0":    round(float(f0),    2), "f0125": round(float(f0125), 2),
            "f025":  round(float(f025),  2), "f050":  round(float(f050),  2),
            "f075":  round(float(f075),  2), "f0875": round(float(f0875), 2),
            "f1":    round(float(f1),    2),
        },
        "current_signal": current,
    }


def _bar_label(df, i: int) -> str:
    """Return human-readable bar label: date for daily, datetime for intraday."""
    ts = df.index[i]
    if hasattr(ts, "hour") and ts.hour != 0:
        return ts.strftime("%Y-%m-%d %H:%M")
    return ts.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════════
# Intraday Quality Score (15m only)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """14-period Average True Range (Wilder method)."""
    n = len(high)
    if n < 2:
        return 0.0
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            float(high[i]) - float(low[i]),
            abs(float(high[i]) - float(close[i - 1])),
            abs(float(low[i])  - float(close[i - 1])),
        )
    # Use last `period` bars average (simple, matches yfinance ATR approx)
    tail = tr[max(1, n - period):]
    return float(tail.mean()) if len(tail) > 0 else 0.0


def _intraday_quality(
    raw,                  # full OHLCV DataFrame (15m bars)
    cur_sig: dict,        # the current_signal dict (may be None)
    cur: float,           # latest close price
    day_chg: float,       # today's % change
) -> dict:
    """
    Compute an intraday quality score (0–100) for a 15-min signal.

    Components
    ----------
    Freshness   (40 pts): bars_ago ≤ 2 → 40 | ≤ 5 → 28 | ≤ 10 → 14 | else 3
    Volume Surge(30 pts): today vol vs time-adjusted historical daily average
    ATR Potential(20 pts): 14-period ATR as % of price (≥ 0.5 % per bar → 20)
    Momentum    (10 pts): day_chg aligned with signal direction

    Returns the score dict.  score = 0 when there is no current signal.
    """
    EMPTY = {"score": 0, "vol_ratio": 0.0, "atr_pct": 0.0,
             "score_fresh": 0, "score_vol": 0, "score_atr": 0, "score_momentum": 0}

    if cur_sig is None or cur <= 0:
        return EMPTY

    # ── Freshness (40 pts) ────────────────────────────────────────────────────
    bars_ago = int(cur_sig.get("bars_ago", 99))
    if   bars_ago <= 2:  fresh = 40
    elif bars_ago <= 5:  fresh = 28
    elif bars_ago <= 10: fresh = 14
    else:                fresh = 3

    # ── Volume Surge (30 pts) ─────────────────────────────────────────────────
    # NSE 15m session: 9:15–15:30 = 375 min = 25 bars per day
    NSE_BARS_PER_DAY = 25
    try:
        today_date = raw.index[-1].date()
        today_mask = np.array([idx.date() == today_date for idx in raw.index])
        today_vol  = float(raw["volume"][today_mask].sum())

        # Historical daily volume sums (exclude today)
        past_mask  = np.array([idx.date() < today_date for idx in raw.index])
        past_raw   = raw[past_mask]
        if len(past_raw) > 0:
            import pandas as pd
            daily_vols = past_raw["volume"].groupby(past_raw.index.date).sum()
            avg_daily  = float(daily_vols.mean()) if len(daily_vols) > 0 else 0.0
        else:
            avg_daily = 0.0

        # Scale avg by how much of the trading day has elapsed
        bars_today   = int(today_mask.sum())
        time_factor  = min(bars_today / NSE_BARS_PER_DAY, 1.0)
        adj_avg      = avg_daily * time_factor if time_factor > 0 else avg_daily

        vol_ratio  = round(today_vol / adj_avg, 2) if adj_avg > 0 else 1.0
        # 1× avg = 0 pts, 2× = 30 pts, linear
        vol_score  = min(30, max(0, int((vol_ratio - 1.0) * 30)))
    except Exception:
        vol_ratio, vol_score = 1.0, 0

    # ── ATR Potential (20 pts) ────────────────────────────────────────────────
    try:
        h = raw["high"].to_numpy(dtype=float)
        l = raw["low"].to_numpy(dtype=float)
        c = raw["close"].to_numpy(dtype=float)
        atr     = _compute_atr(h, l, c, 14)
        atr_pct = round(atr / cur * 100, 3) if cur > 0 else 0.0
        # 0.3 % per 15m bar → solid; 0.5 % → great
        atr_score = min(20, max(0, int(atr_pct / 0.5 * 20)))
    except Exception:
        atr_pct, atr_score = 0.0, 0

    # ── Momentum Alignment (10 pts) ───────────────────────────────────────────
    sig_type = cur_sig.get("type", "")
    if sig_type == "BUY"  and day_chg > 0:
        mom = min(10, max(0, int(day_chg * 5)))
    elif sig_type == "SELL" and day_chg < 0:
        mom = min(10, max(0, int(abs(day_chg) * 5)))
    else:
        mom = 0

    total = fresh + vol_score + atr_score + mom

    return {
        "score":          total,
        "vol_ratio":      vol_ratio,
        "atr_pct":        atr_pct,
        "score_fresh":    fresh,
        "score_vol":      vol_score,
        "score_atr":      atr_score,
        "score_momentum": mom,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Daily Momentum Score (1d only)
# ═══════════════════════════════════════════════════════════════════════════════
#
# The fib-bounce + Gann-angle logic above is purely geometric — a BUY/SELL can
# fire on a stock that is drifting through the level on a broader counter-trend
# or on thin volume, with nothing checking whether the move actually has
# momentum behind it. This scores every daily signal the same way the 15m
# panel already scores intraday signals, so weak, unconfirmed hits can be told
# apart from ones with real trend + volume behind them.

def _ema(series: "pd.Series", period: int) -> "pd.Series":
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: "pd.Series", period: int = 14) -> "pd.Series":
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def _daily_momentum(
    raw,                  # full OHLCV DataFrame (daily bars)
    cur_sig: dict,        # the current_signal dict (may be None)
    cur: float,           # latest close price
    day_chg: float,       # today's % change (unused here, kept for a consistent signature with _intraday_quality)
) -> dict:
    """
    Compute a daily momentum score (0–100) for a swing signal.

    Components
    ----------
    Freshness    (25 pts): bars_ago ≤ 2 → 25 | ≤ 5 → 18 | ≤ 10 → 10 | else 3
    Trend Align  (30 pts): EMA20 vs EMA50 + price vs EMA50, matching signal direction
    RSI Momentum (25 pts): RSI14 in the "healthy" zone for the signal direction —
                            not so weak it's not moving, not so extended it's exhausted
    Volume Confirm(20 pts): latest volume vs its own trailing 20-session average

    Returns the score dict.  score = 0 when there is no current signal.
    """
    EMPTY = {"score": 0, "rsi": None, "trend": None, "vol_ratio": 0.0,
             "score_fresh": 0, "score_trend": 0, "score_rsi": 0, "score_vol": 0}

    if cur_sig is None or cur <= 0 or len(raw) < 55:
        return EMPTY

    close     = raw["close"]
    direction = cur_sig.get("type")

    # ── Freshness (25 pts) ────────────────────────────────────────────────────
    bars_ago = int(cur_sig.get("bars_ago", 99))
    if   bars_ago <= 2:  fresh = 25
    elif bars_ago <= 5:  fresh = 18
    elif bars_ago <= 10: fresh = 10
    else:                fresh = 3

    # ── Trend Alignment (30 pts) ──────────────────────────────────────────────
    try:
        ema20 = float(_ema(close, 20).iloc[-1])
        ema50 = float(_ema(close, 50).iloc[-1])
        trend_label = "up" if ema20 > ema50 else "down"
        if direction == "BUY":
            trend_up, price_up = ema20 > ema50, cur > ema50
            trend_score = 30 if (trend_up and price_up) else 15 if (trend_up or price_up) else 0
        else:
            trend_dn, price_dn = ema20 < ema50, cur < ema50
            trend_score = 30 if (trend_dn and price_dn) else 15 if (trend_dn or price_dn) else 0
    except Exception:
        trend_score, trend_label = 0, None

    # ── RSI Momentum (25 pts) ─────────────────────────────────────────────────
    try:
        rsi_val = float(_rsi(close, 14).iloc[-1])
        if direction == "BUY":
            if   45 <= rsi_val <= 70: rsi_score = 25   # healthy bullish momentum
            elif 70 < rsi_val <= 80:  rsi_score = 12   # overbought — momentum may be late
            elif 35 <= rsi_val < 45:  rsi_score = 12   # still building
            else:                     rsi_score = 0
        else:
            if   30 <= rsi_val <= 55: rsi_score = 25   # healthy bearish momentum
            elif 20 <= rsi_val < 30:  rsi_score = 12   # oversold — momentum may be late
            elif 55 < rsi_val <= 65:  rsi_score = 12   # still building
            else:                     rsi_score = 0
    except Exception:
        rsi_val, rsi_score = None, 0

    # ── Volume Confirmation (20 pts) ──────────────────────────────────────────
    try:
        vol       = float(raw["volume"].iloc[-1])
        avg_vol20 = float(raw["volume"].tail(20).mean())
        vol_ratio = round(vol / avg_vol20, 2) if avg_vol20 > 0 else 0.0
        # < 0.8x avg = 0 pts, 1.5x+ avg = full 20 pts, linear between
        vol_score = min(20, max(0, int((vol_ratio - 0.8) / 0.7 * 20)))
    except Exception:
        vol_ratio, vol_score = 0.0, 0

    total = fresh + trend_score + rsi_score + vol_score

    return {
        "score":       total,
        "rsi":         round(rsi_val, 1) if rsi_val is not None else None,
        "trend":       trend_label,
        "vol_ratio":   vol_ratio,
        "score_fresh": fresh,
        "score_trend": trend_score,
        "score_rsi":   rsi_score,
        "score_vol":   vol_score,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Gann Time Cycle detection (swing-based)
# ═══════════════════════════════════════════════════════════════════════════════

def _gann_pt_square(
    sw_high_bar: int,
    sw_low_bar:  int,
    cur_bar:     int,
    timeframe:   str,
) -> dict:
    """
    Gann Time Cycle detector — purely time-based, no Sq9, no direct P=T.

    Measures how close the bars elapsed since the swing end are to a canonical
    Gann cycle number.  Score (0–100) decays linearly away from each cycle.

    Daily cycles : 45, 90, 144, 180, 270, 360 bars  (tolerance ±5 bars)
    15m cycles   : 45, 90, 135, 180, 225, 270 bars  (tolerance ±5 bars)

    Badges
    ──────
    SQUARE ≥ 60 → within ±5 bars of a Gann cycle  (momentum window)
    NEAR   ≥ 30 → within ±10 bars                 (approaching)
    """
    EMPTY = {"score": 0, "nearest_cycle": None, "bars_from_pivot": None, "cycle_gap": None}

    sw_end          = max(sw_high_bar, sw_low_bar)
    bars_from_pivot = cur_bar - sw_end
    if bars_from_pivot < 0:
        return EMPTY

    if timeframe == "15m":
        cycles    = [45, 90, 135, 180, 225, 270]
        tolerance = 5
    else:
        cycles    = [45, 90, 144, 180, 270, 360]
        tolerance = 5

    best_gap      = min(abs(bars_from_pivot - c) for c in cycles)
    nearest_cycle = min(cycles, key=lambda c: abs(bars_from_pivot - c))

    if   best_gap == 0:                 score = 100
    elif best_gap <= tolerance:         score = int(100 - best_gap / tolerance * 40)         # 100→60
    elif best_gap <= tolerance * 2:     score = int(60 - (best_gap - tolerance) / tolerance * 30)  # 60→30
    elif best_gap <= tolerance * 3:     score = int(30 - (best_gap - tolerance*2) / tolerance * 20) # 30→10
    else:                               score = max(0, 10 - (best_gap - tolerance*3))

    return {
        "score":           max(0, score),
        "nearest_cycle":   nearest_cycle,
        "bars_from_pivot": bars_from_pivot,
        "cycle_gap":       best_gap,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Gann Intraday Score — pure price/time, TODAY's session only
# ═══════════════════════════════════════════════════════════════════════════════

def _intraday_gann(raw, cur_price: float) -> dict:
    """
    Gann Intraday Time Score (0–100) — purely based on session position.
    No Sq9, no direct P=T.  Only criterion: is the current bar at/near a
    natural Gann time division of the NSE trading day?

    NSE day = 25 bars (9:15–15:30).
    Gann natural turns = 1/8 … 7/8 of 25 bars:

        bar 3  → 10:00    bar 12 → 12:15
        bar 6  → 10:45    bar 16 → 13:15
        bar 9  → 11:30    bar 19 → 14:00
                           bar 22 → 14:45

    Score decay (gap = bars from nearest turn):
        gap = 0 → 100   PRIME
        gap = 1 →  75   PRIME
        gap = 2 →  45   SETUP
        gap = 3 →  20
        gap ≥ 4 →   5   (non-zero so always plotted)

    Badges
    ──────
    PRIME ≥ 70 → at or 1 bar off a Gann turn — highest momentum probability
    SETUP ≥ 45 → 2 bars off    — trade window opening/closing
    """
    EMPTY = {
        "score": 0, "bars_since_open": None,
        "nearest_time_level": None, "time_gap": None,
    }

    if cur_price <= 0 or len(raw) < 2:
        return EMPTY

    today_date = raw.index[-1].date()
    today_mask = np.array([idx.date() == today_date for idx in raw.index])
    today      = raw[today_mask]

    if len(today) < 2:
        return EMPTY

    bars_since_open = len(today) - 1

    NSE_DAY_BARS = 25
    gann_t = [round(NSE_DAY_BARS / 8 * k) for k in range(1, 8)]  # [3,6,9,12,16,19,22]

    best_gap  = min(abs(bars_since_open - t) for t in gann_t)
    nearest_t = min(gann_t, key=lambda t: abs(bars_since_open - t))

    _scores = {0: 100, 1: 75, 2: 45, 3: 20}
    score   = _scores.get(best_gap, max(5, 20 - (best_gap - 3) * 5))

    _t_labels = {
        3: "10:00", 6: "10:45",  9: "11:30", 12: "12:15",
        16: "13:15", 19: "14:00", 22: "14:45",
    }

    return {
        "score":              score,
        "bars_since_open":    bars_since_open,
        "nearest_time_level": _t_labels.get(nearest_t, f"bar {nearest_t}"),
        "time_gap":           best_gap,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Per-symbol fetch + scan
# ═══════════════════════════════════════════════════════════════════════════════

def _asset_cfg_override(symbol: str, base_cfg: dict) -> dict:
    """
    Return a (possibly modified) copy of base_cfg suited to the asset class.
    Crypto swings are larger → higher min_rng_pct so tiny swings are skipped.
    Gold (XAUUSD) swings ~1-3 % daily → moderate threshold like equities.
    Regular forex pairs are very tight → lower min_rng_pct (fractions of a percent).
    Commodities (MCX) use the same as equities — 1 % is fine.
    """
    if symbol.endswith("-USD"):          # crypto
        return {**base_cfg, "min_rng_pct": 3.0 if "1d" in base_cfg.get("yf_interval","1d") else 1.5}
    if symbol == "XAUUSD=X":            # gold — behaves more like an equity/commodity
        return {**base_cfg, "min_rng_pct": 1.0 if "1d" in base_cfg.get("yf_interval","1d") else 0.4}
    if symbol.endswith("=X"):           # regular forex pairs — tight ranges
        return {**base_cfg, "min_rng_pct": 0.5 if "1d" in base_cfg.get("yf_interval","1d") else 0.15}
    return base_cfg                     # NSE / MCX — keep defaults


def _scan_symbol(
    symbol: str,
    name: str,
    timeframe: str,
    bos_mode:   str = "Both",
    swing_mode: str = "Largest Range",
    lb_bars:    "int | None" = None,
) -> dict | None:
    cfg = _asset_cfg_override(symbol, TF_CONFIG[timeframe])
    try:
        tk  = yf.Ticker(symbol)
        raw = tk.history(period=cfg["yf_period"], interval=cfg["yf_interval"],
                         auto_adjust=True)
        if raw.empty or len(raw) < cfg["piv_len"] * 2 + 10:
            return None

        raw.columns = [c.lower() for c in raw.columns]
        # Forex / commodity tickers may not have volume — fill 0 so dropna() doesn't strip rows
        for col in ["open", "high", "low", "close"]:
            if col not in raw.columns:
                return None
        if "volume" not in raw.columns:
            raw["volume"] = 0
        raw = raw[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )

        result = compute_signals(raw, cfg, bos_mode=bos_mode, swing_mode=swing_mode,
                                 lb_bars=lb_bars)
        if result["fib_levels"] is None:
            return None

        cur = float(raw["close"].iloc[-1])
        prev = float(raw["close"].iloc[-2]) if len(raw) > 1 else cur
        day_chg = round((cur - prev) / prev * 100, 2) if prev else 0.0

        # ── Price-position validity ───────────────────────────────────────────
        # For each recent signal, check whether the CURRENT price is still on
        # the correct side of the trigger level.
        #   BUY  → price must still be ABOVE 1/8 (f0125)
        #   SELL → price must still be BELOW 7/8 (f0875)
        # If price has already crossed back, the signal is stale (⚠).
        def _price_ok(sig: dict) -> bool:
            # Use float() on both sides — sig["f0125/f0875"] may be numpy.float64
            # which makes the comparison return numpy.bool_ (not JSON-serialisable).
            if sig["type"] == "BUY":
                return bool(cur > float(sig["f0125"]))
            else:
                return bool(cur < float(sig["f0875"]))

        # Annotate current_signal and recent_signals with price_still_valid
        cur_sig = result["current_signal"]
        if cur_sig is not None:
            cur_sig = {**cur_sig, "price_still_valid": _price_ok(cur_sig)}

        recent_sigs = [
            {**s, "price_still_valid": _price_ok(s)}
            for s in result["recent_signals"]
        ]

        # ── Quality / momentum score — timeframe-specific formula ────────────
        # 15m: freshness + intraday volume surge + ATR potential + day-change
        # 1d : freshness + EMA trend alignment + RSI zone + 20-session volume
        quality = (
            _intraday_quality(raw, cur_sig, cur, day_chg)
            if timeframe == "15m" else _daily_momentum(raw, cur_sig, cur, day_chg)
        )

        # ── Gann Intraday Score (15m only — today's time+price convergence) ──
        gann_intraday = (
            _intraday_gann(raw, cur)
            if timeframe == "15m" else {"score": None}
        )

        # ── Gann Price/Time Square detection (all timeframes) ─────────────
        fibs    = result["fib_levels"]
        pt_sq   = _gann_pt_square(
            sw_high_bar = fibs["swing_high_bar"],
            sw_low_bar  = fibs["swing_low_bar"],
            cur_bar     = len(raw) - 1,
            timeframe   = timeframe,
        )

        return {
            "symbol":         symbol,
            "name":           name,
            "current_price":  round(cur, 2),
            "day_change_pct": day_chg,
            "fib_levels":     result["fib_levels"],
            "current_signal": cur_sig,
            "recent_signals": recent_sigs,
            "last_signals":   result["signals"][-5:],
            "quality":        quality,
            "pt_square":      pt_sq,
            "gann_intraday":  gann_intraday,
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Background job runner
# ═══════════════════════════════════════════════════════════════════════════════

def _run_scan_thread(
    job_id: str,
    symbols: dict[str, str],
    timeframe: str,
    bos_mode:   str = "Both",
    swing_mode: str = "Largest Range",
    lb_bars:    "int | None" = None,
) -> None:
    sym_list = list(symbols.items())
    total    = len(sym_list)
    results  = {"buy": [], "sell": [], "all": []}
    done     = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_scan_symbol, sym, name, timeframe, bos_mode, swing_mode, lb_bars): (sym, name)
            for sym, name in sym_list
        }

        for fut in as_completed(futures):
            done += 1
            with _lock:
                _jobs[job_id]["progress"] = done

            row = fut.result()
            if row is None:
                continue

            results["all"].append(row)
            if row["current_signal"]:
                t = row["current_signal"]["type"]
                if t == "BUY":
                    results["buy"].append(row)
                elif t == "SELL":
                    results["sell"].append(row)

    for key in ("buy", "sell"):
        results[key].sort(key=lambda r: r["current_signal"]["bars_ago"])

    results["scanned_at"] = datetime.now(timezone.utc).isoformat()
    results["timeframe"]  = timeframe
    results["tf_label"]   = TF_CONFIG[timeframe]["label"]
    results["bos_mode"]   = bos_mode
    results["swing_mode"] = swing_mode
    results["lb_bars"]    = lb_bars if lb_bars is not None else TF_CONFIG[timeframe]["lb_bars"]

    _lb_key   = lb_bars if lb_bars is not None else "default"
    cache_key = f"{_jobs[job_id]['universe']}_{timeframe}_{bos_mode}_{swing_mode}_{_lb_key}"
    with _lock:
        _jobs[job_id]["status"]  = "done"
        _jobs[job_id]["results"] = results
        _cache[cache_key] = {"results": results, "ts": time.time()}


def start_scan(
    universe:       str = "nifty50",
    timeframe:      str = "1d",
    bos_mode:       str = "Both",          # "Simple" | "Strict" | "Both"
    swing_mode:     str = "Largest Range", # "Largest Range" | "Most Recent"
    custom_symbols: "dict[str, str] | None" = None,
    lb_bars:        "int | None" = None,   # None → use TF_CONFIG default (200)
) -> str:
    """
    Start a background signal scan.  Parameters map 1-to-1 with Pine Script v3.2 inputs.
    Results are cached; different parameter combos get separate cache entries.

    custom_symbols: when provided (e.g. for ATH-derived universes), bypasses the
    UNIVERSES lookup and scans exactly these {symbol: name} pairs. The universe
    string is still used as the cache-key prefix.

    lb_bars: overrides the TF_CONFIG lookback bars for "Largest Range" mode.
    When None, the default (200 for daily, 200 for 15m) is used — identical to
    existing behaviour, so no cached results are invalidated.
    """
    _lb_key   = lb_bars if lb_bars is not None else "default"
    cache_key = f"{universe}_{timeframe}_{bos_mode}_{swing_mode}_{_lb_key}"
    ttl       = CACHE_TTL.get(timeframe, 3600)

    with _lock:
        cached = _cache.get(cache_key)
        if cached and time.time() - cached["ts"] < ttl:
            new_id    = str(uuid.uuid4())
            # derive count from actual results (works for ATH universes too)
            res       = cached.get("results") or {}
            sym_count = len(res.get("all", []))
            _jobs[new_id] = {
                "status": "done", "progress": sym_count,
                "total": sym_count, "universe": universe,
                "timeframe": timeframe, "bos_mode": bos_mode,
                "swing_mode": swing_mode, "lb_bars": lb_bars,
                "results": cached["results"],
            }
            return new_id

    # Resolve symbol list
    if custom_symbols is not None:
        symbols = custom_symbols
    else:
        symbols = {s: ALL_SYMBOLS[s] for s in UNIVERSES.get(universe, []) if s in ALL_SYMBOLS}

    job_id  = str(uuid.uuid4())

    with _lock:
        _jobs[job_id] = {
            "status": "running", "progress": 0,
            "total": len(symbols), "universe": universe,
            "timeframe": timeframe, "bos_mode": bos_mode,
            "swing_mode": swing_mode, "lb_bars": lb_bars,
            "results": None,
        }

    t = threading.Thread(
        target=_run_scan_thread,
        args=(job_id, symbols, timeframe, bos_mode, swing_mode, lb_bars),
        daemon=True,
    )
    t.start()
    return job_id


def get_status(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {
        "status":    job["status"],
        "progress":  job["progress"],
        "total":     job["total"],
        "timeframe": job.get("timeframe", "1d"),
        "results":   job.get("results"),
    }
