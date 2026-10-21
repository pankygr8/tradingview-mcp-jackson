"""
pricetime.py — experimental Gann "squaring of price and time" scanner.

Gann's idea: when the distance price has travelled from a major swing pivot
(measured in a natural price unit) equals the time elapsed since that pivot
(in bars), price and time are "squared" and a change in trend is due.

Rule used here (daily bars, nothing smoothed):
  anchor      the more recent of the highest high / lowest low of the last
              120 sessions (the major swing pivot)
  t           sessions elapsed since the anchor (must be 5–90)
  unit        median daily range (High−Low) of the last 20 sessions — one
              "Gann unit" of price for THIS stock, so the rule works at any
              price level
  u           |close − anchor| / unit   (price travelled, in units)
  square      |u − t| <= max(1, 10% of t)      → price and time are equal
              ratio u/t is kept so other angles (2x1, 1x2) can be studied

Buckets (all three are recorded so the data decides which way it works):
  pt_reversal            trade AGAINST the move (Gann's "change in trend")
  pt_reversal_confirmed  same, but only if today's bar already turned
                         (closed against the move and below/above its open)
  pt_continuation        trade WITH the move (the control group)

There is no native stop/target; every signal is graded with the uniform plan
(1.5×ATR stop, 3×ATR target, 10-bar time stop) like the other no-plan scanners.
"""

from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf  # replaced by a point-in-time fake during replay

LOOKBACK = 120
UNIT_BARS = 20
MIN_T, MAX_T = 5, 90
TOL_FRAC = 0.10


def check(symbol: str) -> dict | None:
    try:
        df = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
        if df is None or len(df) < LOOKBACK + 5:
            return None
        df = df.dropna(subset=["High", "Low", "Close", "Open"]).tail(LOOKBACK)
        H, L, C, O = (df[c].tolist() for c in ("High", "Low", "Close", "Open"))
        n = len(df)
        hi_i = max(range(n), key=lambda i: H[i])
        lo_i = min(range(n), key=lambda i: L[i])
        if hi_i > lo_i:
            anchor_i, anchor_px, from_high = hi_i, H[hi_i], True
        else:
            anchor_i, anchor_px, from_high = lo_i, L[lo_i], False
        t = n - 1 - anchor_i
        if not (MIN_T <= t <= MAX_T):
            return None
        unit = statistics.median(h - l for h, l in zip(H[-UNIT_BARS:], L[-UNIT_BARS:]))
        if unit <= 0:
            return None
        close = C[-1]
        u = abs(close - anchor_px) / unit
        gap = abs(u - t)
        square = gap <= max(1.0, TOL_FRAC * t)
        # move direction since the anchor: from a high the move is down, from a low it is up
        move = "down" if from_high else "up"
        turned = (close > C[-2] and close > O[-1]) if move == "down" else (close < C[-2] and close < O[-1])
        return {
            "symbol": symbol, "close": round(close, 2), "square": square,
            "move": move, "turned": bool(turned),
            "bars_from_pivot": t, "units_moved": round(u, 2), "pt_ratio": round(u / t, 3),
            "pt_gap": round(gap, 2), "anchor": round(anchor_px, 2), "unit": round(unit, 4),
            "unit_pct": round(unit / close * 100, 3),
        }
    except Exception:
        return None


def scan(symbols: list[str]) -> dict:
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [r for r in pool.map(check, symbols) if r]
    return {"all": rows, "square": [r for r in rows if r["square"]]}
