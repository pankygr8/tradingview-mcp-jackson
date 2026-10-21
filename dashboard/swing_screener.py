"""
swing_screener.py — NSE swing screener (5–7 day horizon), Gann-based.

Philosophy
──────────
Same rule as the intraday engine: NO lagging indicators. No moving averages,
no RSI/MACD/stochastics. Everything here is one of:

  • Static structure   — swing pivots, prior ranges, 52w extremes, Gann Sq9
                         ladder anchored on the last swing pivot.
  • Coincident state   — today's range, volume, position within the range.
  • Pure-price ratios  — relative strength vs the index (no smoothing applied
                         to the ratio; it is a raw two-point return spread).

What makes it a SWING engine rather than an intraday one
────────────────────────────────────────────────────────
  1. Anchor      → the last confirmed swing low/high (fractal pivot), not the
                   day's open. Gann's own practice: square the swing, not the bar.
  2. Time layer  → Gann's natural bar-counts (7, 14, 21, 30, 45, 60, 90) measured
                   from the pivot. A 5–7 day horizon lives inside the 7/14 cycle.
  3. Setup logic → range contraction (coil) preceding expansion. Contraction is
                   the leading tell; the breakout is the confirmation.
  4. Parameters  → liquidity, relative strength, ADR-based sizing, and an explicit
                   reward:risk computed against real Gann levels — the things that
                   decide whether a 5-day hold is worth taking at all.

Public API mirrors the other modules:
    start_swing_scan(universe) -> job_id
    get_swing_status(job_id)   -> {status, progress, total, results}
"""

from __future__ import annotations

import math
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from screener import UNIVERSES, ALL_SYMBOLS

# ── Config ────────────────────────────────────────────────────────────────────

CACHE_TTL   = 3600           # 1 h — daily bars, no need to hammer
MAX_WORKERS = 10
BENCHMARK   = "^NSEI"        # Nifty 50 index, used for relative strength

HOLD_DAYS       = 6          # midpoint of the 5–7 day horizon
PIVOT_WING      = 3          # fractal: 3 bars each side confirms a pivot
COIL_WINDOW     = 5          # recent bars whose range we compare…
COIL_BASE       = 20         # …against this longer base
RS_FAST, RS_SLOW = 21, 63    # relative-strength lookbacks (bars)
MIN_TURNOVER_CR = 5.0        # ₹ crore median daily turnover — tradeability floor

# Gann's natural time cycles (bars from pivot). ±1 bar tolerance.
GANN_CYCLES = [7, 14, 21, 30, 45, 60, 90]

# ── Time stop ─────────────────────────────────────────────────────────────────
# Gann's exit principle: a move that has not materialised by its cycle date has
# failed, whatever the price is doing. Price-based stops answer "am I wrong?".
# The time stop answers "was I wrong to be here at all?" — and it fires without
# waiting for price to prove it, which is why it exits earlier than any indicator.
MIN_TIME_BUDGET  = 3      # bars; a cycle boundary closer than this is skipped
TRADE_CYCLE      = 7      # every trade gets its own first Gann cycle from entry
MAX_TIME_BUDGET  = 14     # bars; two Gann weeks, ceiling for a 5–7 day horizon
HALF_LIFE_R      = 0.5    # by half the budget, the trade must show this much R

# Net travel over n bars ≈ ADR · n^p.
#   p = 0.50 → random walk. Correct when direction is NOT confirmed (a coil).
#   p = 0.75 → drifting. Correct after a break of structure, which is precisely
#              the claim that drift is no longer zero. Using the random-walk
#              exponent on a confirmed-trend setup would reject the very trades
#              the structure filter exists to find.
TRAVEL_EXP_TREND = 0.75
TRAVEL_EXP_RANGE = 0.50


def _add_trading_days(d, n: int):
    """Approximate calendar date n trading days ahead (weekends only, no holidays)."""
    try:
        return (pd.bdate_range(start=d, periods=n + 1)[-1]).date()
    except Exception:
        return None


def _time_budget(bars_since_anchor: int | None) -> tuple[int, int | None]:
    """
    Bars of runway. Two clocks: the STRUCTURE's clock (bars to the pivot's next
    Gann cycle) and the TRADE's clock (its own 7-bar cycle from entry). The trade
    gets the more generous of the two, capped — a position opened at bar 6 of a
    7-bar cycle is not obliged to be right within one session.
    """
    if bars_since_anchor is None:
        return TRADE_CYCLE, None
    rem, cyc = MAX_TIME_BUDGET, None
    for c in GANN_CYCLES:
        r = c - bars_since_anchor
        if r >= MIN_TIME_BUDGET:
            rem, cyc = r, c
            break
    return min(max(rem, TRADE_CYCLE), MAX_TIME_BUDGET), cyc

# Sq9 rung calibration: aim for one rung ≈ ADR% × this, so a rung is roughly
# one day's travel — the natural unit for a 5–7 day hold.
RUNG_PER_ADR = 1.0
RUNG_FLOOR, RUNG_CEIL = 0.5, 4.0
DEG_MIN, DEG_MAX = 3.0, 180.0

_cache: dict[str, dict] = {}
_jobs:  dict[str, dict] = {}
_lock = threading.Lock()


# ── Gann geometry ─────────────────────────────────────────────────────────────

def _pick_degree_step(price: float, target_rung_pct: float) -> float:
    """
    One 360° Sq9 rotation = +2.0 on the square root, so a root-step `s` moves
    price by ≈ 2·√price·s. Solving for the step that yields target_rung_pct:
        degrees ≈ 0.9 · target_pct · √price
    """
    if price <= 0:
        return 45.0
    return max(DEG_MIN, min(DEG_MAX, 0.9 * target_rung_pct * math.sqrt(price)))


def gann_sq9_ladder(anchor: float, degree_step: float, n_side: int = 8) -> list[float]:
    """True Square of 9: (√anchor ± i·step)², step in degrees (180° = 1.0 on root)."""
    if anchor <= 0:
        return []
    root = math.sqrt(anchor)
    s = degree_step / 180.0
    out = {round(anchor, 2)}
    for i in range(1, n_side + 1):
        out.add(round((root + i * s) ** 2, 2))
        if root - i * s > 0:
            out.add(round((root - i * s) ** 2, 2))
    return sorted(out)


def gann_levels(anchor: float, current: float, degree_step: float) -> dict:
    ladder = gann_sq9_ladder(anchor, degree_step)
    if not ladder:
        return {}
    res = [l for l in ladder if l > current]
    sup = [l for l in ladder if l < current]
    return {
        "anchor":       round(anchor, 2),
        "degree_step":  round(degree_step, 2),
        "targets_up":   res[:3],
        "targets_down": list(reversed(sup[-3:])),
        "nearest_resistance": res[0]  if res else None,
        "nearest_support":    sup[-1] if sup else None,
        "ladder": ladder,
    }


# ── Structure: fractal swing pivots ──────────────────────────────────────────

def _find_pivots(highs: list[float], lows: list[float], wing: int = PIVOT_WING):
    """
    Return (last_pivot_high, bars_since_ph, last_pivot_low, bars_since_pl).
    A pivot high = a bar whose high exceeds `wing` bars on both sides. Confirmed,
    therefore lagging by `wing` bars by construction — but it is STRUCTURE, not a
    smoothed signal: the level itself is exact and never repaints once formed.
    """
    n = len(highs)
    ph = pl = None
    ph_i = pl_i = None
    for i in range(n - wing - 1, wing - 1, -1):
        if ph is None and all(highs[i] > highs[i - k] for k in range(1, wing + 1)) \
                      and all(highs[i] > highs[i + k] for k in range(1, wing + 1)):
            ph, ph_i = highs[i], i
        if pl is None and all(lows[i] < lows[i - k] for k in range(1, wing + 1)) \
                      and all(lows[i] < lows[i + k] for k in range(1, wing + 1)):
            pl, pl_i = lows[i], i
        if ph is not None and pl is not None:
            break
    return (ph, (n - 1 - ph_i) if ph_i is not None else None,
            pl, (n - 1 - pl_i) if pl_i is not None else None)


def _gann_cycle_flag(bars: int | None) -> tuple[str | None, int | None]:
    """Is the bar-count from the pivot sitting on a Gann natural cycle (±1)?"""
    if bars is None:
        return None, None
    for c in GANN_CYCLES:
        if abs(bars - c) <= 1:
            return f"cycle_{c}", c
    return None, None


# ── Market structure: Gann's swing chart (HH / HL / LH / LL) ─────────────────
#
# Gann's own "trend indicator" was the swing chart: a series of higher tops and
# higher bottoms is an uptrend; lower tops and lower bottoms is a downtrend. The
# sequence is what carries the information — a single pivot tells you nothing.
#
# Once the sequence is known, two further things fall out for free:
#   • BREAK OF STRUCTURE (BOS) — price takes out the prior swing high. That is
#     the trend *confirming*, not the entry.
#   • THE PULLBACK — the retracement that follows the BOS, back into Gann's
#     eighths (3/8 – 5/8 of the impulse leg, with 4/8 = 50% the decision point).
#     THAT is the entry: tight stop under the higher low, targets far above.
#
# Entering the BOS itself gives a wide stop and poor R:R. Entering the pullback
# to the higher low is the same trade at a third of the risk.

GANN_EIGHTHS = {0.375: "3/8", 0.500: "4/8", 0.625: "5/8"}
PULLBACK_LO, PULLBACK_HI = 0.30, 0.68     # tolerance around the 3/8–5/8 band
BOS_MAX_AGE = 12                          # bars; a stale BOS is not actionable


def _swing_sequence(highs: list[float], lows: list[float], wing: int = PIVOT_WING) -> list[dict]:
    """
    All confirmed fractal pivots in chronological order, each labelled against
    its predecessor of the same kind: HH/LH for highs, HL/LL for lows.
    Confirmed = `wing` bars on both sides, so a pivot never repaints.
    """
    n = len(highs)
    piv: list[dict] = []
    for i in range(wing, n - wing):
        is_h = all(highs[i] > highs[i - k] for k in range(1, wing + 1)) and \
               all(highs[i] > highs[i + k] for k in range(1, wing + 1))
        is_l = all(lows[i] < lows[i - k] for k in range(1, wing + 1)) and \
               all(lows[i] < lows[i + k] for k in range(1, wing + 1))
        if is_h:
            piv.append({"i": i, "kind": "H", "price": highs[i]})
        elif is_l:
            piv.append({"i": i, "kind": "L", "price": lows[i]})

    last_h = last_l = None
    for p in piv:
        if p["kind"] == "H":
            p["label"] = "HH" if (last_h is not None and p["price"] > last_h) else \
                         ("LH" if last_h is not None else "H")
            last_h = p["price"]
        else:
            p["label"] = "HL" if (last_l is not None and p["price"] > last_l) else \
                         ("LL" if last_l is not None else "L")
            last_l = p["price"]
    return piv


def _structure_state(piv: list[dict], highs: list[float], lows: list[float],
                     closes: list[float]) -> dict | None:
    """
    Derive trend, break of structure, the impulse leg, and where price sits
    inside that leg's Gann-eighths retracement.
    """
    if len(piv) < 4:
        return None
    n = len(closes)
    close = closes[-1]

    hs = [p for p in piv if p["kind"] == "H"]
    ls = [p for p in piv if p["kind"] == "L"]
    if len(hs) < 2 or len(ls) < 2:
        return None

    last_h, prev_h = hs[-1], hs[-2]
    last_l, prev_l = ls[-1], ls[-2]

    bullish = last_h["label"] == "HH" and last_l["label"] == "HL"
    bearish = last_h["label"] == "LH" and last_l["label"] == "LL"
    trend = "bullish" if bullish else ("bearish" if bearish else "range")

    # ── Break of structure: first bar closing beyond the PRIOR swing high/low
    bos_i = bos_dir = None
    if trend == "bullish":
        for i in range(prev_h["i"] + 1, n):
            if closes[i] > prev_h["price"]:
                bos_i, bos_dir = i, "up"
                break
    elif trend == "bearish":
        for i in range(prev_l["i"] + 1, n):
            if closes[i] < prev_l["price"]:
                bos_i, bos_dir = i, "down"
                break
    bos_age = (n - 1 - bos_i) if bos_i is not None else None

    # ── Impulse leg: the swing that BOS created, and today's retracement of it
    leg_lo = leg_hi = retr = None
    if bos_dir == "up":
        leg_lo = last_l["price"]                       # the higher low
        leg_hi = max(highs[last_l["i"]:])              # the high made since it
        if leg_hi > leg_lo:
            retr = (leg_hi - close) / (leg_hi - leg_lo)
    elif bos_dir == "down":
        leg_hi = last_h["price"]                       # the lower high
        leg_lo = min(lows[last_h["i"]:])
        if leg_hi > leg_lo:
            retr = (close - leg_lo) / (leg_hi - leg_lo)

    # nearest Gann eighth to the current retracement
    eighth = None
    if retr is not None:
        near = min(GANN_EIGHTHS, key=lambda e: abs(e - retr))
        if abs(near - retr) <= 0.07:
            eighth = GANN_EIGHTHS[near]

    return {
        "trend": trend,
        "labels": [p["label"] for p in piv[-6:]],
        "last_high": round(last_h["price"], 2), "last_high_label": last_h["label"],
        "last_low":  round(last_l["price"], 2), "last_low_label":  last_l["label"],
        "prev_high": round(prev_h["price"], 2), "prev_low": round(prev_l["price"], 2),
        "bos_dir": bos_dir, "bos_age": bos_age,
        "leg_low": round(leg_lo, 2) if leg_lo else None,
        "leg_high": round(leg_hi, 2) if leg_hi else None,
        "retracement": round(retr, 3) if retr is not None else None,
        "gann_eighth": eighth,
        "in_pullback_zone": bool(retr is not None and PULLBACK_LO <= retr <= PULLBACK_HI),
    }


# ── Benchmark (fetched once, shared) ─────────────────────────────────────────

def _fetch_benchmark() -> pd.DataFrame | None:
    try:
        b = yf.Ticker(BENCHMARK).history(period="1y", interval="1d", auto_adjust=True)
        return b.dropna(subset=["Close"]) if b is not None and not b.empty else None
    except Exception:
        return None


def _bench_return(bench: pd.DataFrame | None, bars: int) -> float | None:
    if bench is None or len(bench) < bars + 1:
        return None
    c = bench["Close"]
    return float(c.iloc[-1] / c.iloc[-1 - bars] - 1.0) * 100


# ── Per-symbol swing analysis ────────────────────────────────────────────────

def _check_swing(symbol: str, bench_fast: float | None, bench_slow: float | None) -> dict | None:
    try:
        df = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
        if df is None or len(df) < 120:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 120:
            return None

        H = df["High"].astype(float).tolist()
        L = df["Low"].astype(float).tolist()
        C = df["Close"].astype(float).tolist()
        V = df["Volume"].astype(float).tolist() if "Volume" in df else [0.0] * len(C)

        close = C[-1]
        prev_close = C[-2]
        chg_pct = round((close - prev_close) / prev_close * 100, 2)

        # ── Liquidity gate (₹ crore of median daily turnover) ───────────────
        turnovers = [C[i] * V[i] for i in range(-63, 0)]
        turnover_cr = round(statistics.median(turnovers) / 1e7, 2)  # ₹Cr
        if turnover_cr < MIN_TURNOVER_CR:
            return None

        # ── ADR% — the instrument's own historical travel per day ───────────
        adrs = [(H[i] - L[i]) / C[i] * 100 for i in range(-20, 0) if C[i] > 0]
        adr_pct = round(statistics.mean(adrs), 2) if adrs else 1.0

        # ── Coil / range contraction: the leading tell before expansion ─────
        r_recent = max(H[-COIL_WINDOW:]) - min(L[-COIL_WINDOW:])
        r_base   = max(H[-COIL_BASE:])   - min(L[-COIL_BASE:])
        coil = round(r_recent / r_base, 3) if r_base > 0 else None   # <0.40 = tight

        today_range = H[-1] - L[-1]
        nr7 = today_range <= min(H[i] - L[i] for i in range(-7, 0))
        inside_day = H[-1] <= H[-2] and L[-1] >= L[-2]

        # ── Volume: dry-up in the base, expansion on the move ───────────────
        v_recent = statistics.mean(V[-COIL_WINDOW:]) if V[-1] else 0.0
        v_base   = statistics.mean(V[-COIL_BASE:])   if V[-1] else 0.0
        vol_dryup = round(v_recent / v_base, 2) if v_base > 0 else None  # <0.8 = quiet base
        rvol_d    = round(V[-1] / v_base, 2) if v_base > 0 else None     # today's expansion

        # ── Relative strength vs Nifty (raw return spread, unsmoothed) ──────
        def _ret(bars): return (close / C[-1 - bars] - 1.0) * 100 if len(C) > bars else None
        r_fast, r_slow = _ret(RS_FAST), _ret(RS_SLOW)
        rs_fast = round(r_fast - bench_fast, 2) if (r_fast is not None and bench_fast is not None) else None
        rs_slow = round(r_slow - bench_slow, 2) if (r_slow is not None and bench_slow is not None) else None

        # ── Positional structure ────────────────────────────────────────────
        hi_252, lo_252 = max(H[-252:]), min(L[-252:])
        pct_from_52wh = round((close - hi_252) / hi_252 * 100, 2)
        pos_in_range  = round((close - lo_252) / (hi_252 - lo_252) * 100, 1) if hi_252 > lo_252 else None

        ph, bars_ph, pl, bars_pl = _find_pivots(H, L)
        if ph is None or pl is None:
            return None
        dist_to_ph = round((ph - close) / close * 100, 2)    # small + = coiled under resistance
        dist_to_pl = round((close - pl) / close * 100, 2)
        swing_mid  = round((ph + pl) / 2, 2)                 # Gann 50% of the swing

        # ── Gann: anchor on the pivot the market is working AGAINST ─────────
        uptrend = close >= swing_mid
        anchor  = pl if uptrend else ph
        deg = _pick_degree_step(anchor, max(RUNG_FLOOR, min(RUNG_CEIL, adr_pct * RUNG_PER_ADR)))
        g = gann_levels(anchor, close, deg)

        # ── Gann time cycles from each pivot ────────────────────────────────
        cyc_low,  _ = _gann_cycle_flag(bars_pl)
        cyc_high, _ = _gann_cycle_flag(bars_ph)
        time_cycle = cyc_low or cyc_high

        # ── Market structure (Gann swing chart) ─────────────────────────────
        st = _structure_state(_swing_sequence(H, L), H, L, C)

        # ── Setup classification (structure, not signal) ────────────────────
        # A coil only counts as a real base if volume actually dried up in it.
        # Contraction on *rising* volume is distribution, not accumulation.
        quiet_base = vol_dryup is not None and vol_dryup < 0.85

        # EXTENSION: how far past the pivot has price already travelled, measured
        # in the stock's OWN average daily range. `close > pivot` alone is not a
        # breakout setup — a stock 3 ADR beyond its pivot broke out days ago and
        # the move is largely spent. Entering there is chasing, not breaking out.
        ext_up   = (close - ph) / (adr_pct / 100 * close) if adr_pct > 0 else 0.0
        ext_down = (pl - close) / (adr_pct / 100 * close) if adr_pct > 0 else 0.0
        extension = round(ext_up if close > ph else (ext_down if close < pl else 0.0), 2)

        FRESH, STALE = 0.5, 1.5      # ADR units past the pivot

        # BOS pullback: trend confirmed, then price retraces into Gann's eighths.
        # Checked FIRST — it is the same trade as the breakout, entered at a
        # third of the risk, and it is what a breakout-only screener misses.
        fresh_bos = bool(st and st["bos_age"] is not None and st["bos_age"] <= BOS_MAX_AGE)
        pullback_long  = bool(st and st["trend"] == "bullish" and st["bos_dir"] == "up"
                              and fresh_bos and st["in_pullback_zone"])
        pullback_short = bool(st and st["trend"] == "bearish" and st["bos_dir"] == "down"
                              and fresh_bos and st["in_pullback_zone"])

        setup = "neutral"
        if pullback_long:
            setup = "bos_pullback_long"
        elif pullback_short:
            setup = "bos_pullback_short"
        elif st and fresh_bos and st["bos_dir"] == "up" and st["retracement"] is not None \
                and st["retracement"] < PULLBACK_LO:
            setup = "awaiting_pullback"      # BOS fired, no retrace yet — watch it
        elif coil is not None and coil < 0.40 and abs(dist_to_ph) < adr_pct * 2 and quiet_base:
            setup = "coiled_under_pivot"
        elif close > ph and rvol_d and rvol_d > 1.5:
            if ext_up <= FRESH:
                setup = "pivot_breakout"            # sitting on the trigger
            elif ext_up <= STALE:
                setup = "breakout_extended"         # wait for a pullback to the pivot
            else:
                setup = "chasing"                   # move already spent
        elif close < pl and rvol_d and rvol_d > 1.5:
            if ext_down <= FRESH:
                setup = "pivot_breakdown"
            elif ext_down <= STALE:
                setup = "breakdown_extended"
            else:
                setup = "chasing"
        elif coil is not None and coil < 0.40 and quiet_base:
            setup = "coiled"
        elif coil is not None and coil < 0.40:
            setup = "coiled_noisy"          # tight, but volume never dried up
        elif abs(close - swing_mid) / close * 100 < adr_pct * 0.5:
            setup = "at_gann_midpoint"

        # ── Trade geometry for a 5–7 day hold ───────────────────────────────
        # Expected travel over the horizon, from the stock's own ADR (√time).
        # Rungs are calibrated to ≈1 ADR each, so over ~6 days price should cover
        # SEVERAL rungs. Targeting the *nearest* rung would therefore be far too
        # close and would mechanically crush R:R below 1 on every setup.
        exp_move_pct = round(adr_pct * math.sqrt(HOLD_DAYS), 2)
        ladder = g.get("ladder", [])
        coil_low  = min(L[-COIL_WINDOW:])
        coil_high = max(H[-COIL_WINDOW:])

        # A pullback long sits BELOW the swing midpoint by definition — that is
        # what a retracement is. The structure sequence, not the midpoint, decides
        # direction whenever the two disagree.
        if setup == "bos_pullback_long":
            uptrend = True
        elif setup == "bos_pullback_short":
            uptrend = False

        def _first_beyond(levels: list[float], reach: float, up: bool):
            """First ladder rung at or beyond the expected-move distance."""
            for l in (levels if up else reversed(levels)):
                if (up and l >= reach) or (not up and l <= reach):
                    return l
            return None

        # ENTRY depends on the setup. A coil under the pivot is not entered at
        # the current price — it is entered when the pivot breaks. Measuring
        # reward from `close` instead of the trigger understates every base.
        #
        # STOP depends on the setup too. Once a pivot has broken, the PIVOT is
        # the invalidation level; the old coil low is far below and irrelevant.
        # Using the coil low for a broken-out stock inflates risk and mechanically
        # drives R:R toward (expected_move / distance_travelled) — a circular number
        # that measures how far the stock has run, not whether the trade has edge.
        coil_setups = ("coiled_under_pivot", "coiled", "coiled_noisy")
        brk_up      = ("pivot_breakout", "breakout_extended")
        brk_down    = ("pivot_breakdown", "breakdown_extended")
        pb_up, pb_dn = ("bos_pullback_long",), ("bos_pullback_short",)
        STOP_BUFFER = 0.40      # ADR fraction placed beyond the invalidation level

        if uptrend:
            if setup in pb_up:
                # Entry AT the pullback. The higher low IS the invalidation level —
                # buffer just beneath it rather than dropping to the next Sq9 rung,
                # which on a wide-ADR stock sits far below and destroys the R:R.
                entry = close
                hl    = st["leg_low"]
                stop  = hl * (1 - STOP_BUFFER * adr_pct / 100)
            elif setup in brk_up:
                entry = close
                inval = ph * (1 - adr_pct / 100)
                below = [l for l in ladder if l < inval]
                stop  = below[-1] if below else inval
            else:
                entry = ph if setup in coil_setups else close
                below = [l for l in ladder if l < coil_low]
                stop  = below[-1] if below else coil_low * (1 - adr_pct / 100)
        else:
            if setup in pb_dn:
                entry = close
                lh    = st["leg_high"]
                stop  = lh * (1 + STOP_BUFFER * adr_pct / 100)
            elif setup in brk_down:
                entry = close
                inval = pl * (1 + adr_pct / 100)
                above = [l for l in ladder if l > inval]
                stop  = above[0] if above else inval
            else:
                entry = pl if setup in coil_setups else close
                above = [l for l in ladder if l > coil_high]
                stop  = above[0] if above else coil_high * (1 + adr_pct / 100)

        # ── Targets: R-multiples SNAPPED to Gann Sq9 rungs ──────────────────
        # Adjacent rungs are ~1 ADR apart, so on a wide-ADR stock the next rung up
        # is a trivial 2% target and R:R collapses. Anchoring targets to 1R/2R/3R
        # and then snapping each to the first Sq9 rung beyond it keeps every exit
        # on a real Gann level while guaranteeing the trade pays a multiple of risk.
        risk_abs = abs(entry - stop)
        def _snap(mult: float):
            if risk_abs <= 0:
                return None
            lvl = entry + mult * risk_abs if uptrend else entry - mult * risk_abs
            cands = [l for l in ladder if l >= lvl] if uptrend else [l for l in ladder if l <= lvl]
            if cands:
                return cands[0] if uptrend else cands[-1]
            return round(lvl, 2)

        tp1, tp2, tp3 = _snap(1.0), _snap(2.0), _snap(3.0)
        target = tp1
        # Sanity: TP1 should still be inside a plausible multi-day travel range.
        # If 1R already exceeds ~2.5x the expected 6-day move, the stop is too tight
        # for this instrument's noise and the trade is a lottery ticket.
        reach = entry * (1 + exp_move_pct / 100) if uptrend else entry * (1 - exp_move_pct / 100)
        tp1_unreachable = bool(tp1 and (
            (uptrend and tp1 > entry * (1 + 2.5 * exp_move_pct / 100)) or
            ((not uptrend) and tp1 < entry * (1 - 2.5 * exp_move_pct / 100))))

        entry = round(entry, 2)
        stop  = round(stop, 2) if stop else None
        rr = None
        if target and stop and abs(entry - stop) > 1e-9:
            rr = round(abs(target - entry) / abs(entry - stop), 2)

        # Risk as % of entry — drives sizing, and sanity-checks the stop:
        # wider than ~3.5 ADR is not a 5–7 day risk profile.
        risk_pct = round(abs(entry - stop) / entry * 100, 2) if stop else None
        stop_too_wide = bool(risk_pct and risk_pct > adr_pct * 3.5)
        # How far price must travel just to trigger the entry.
        dist_to_entry_pct = round((entry - close) / close * 100, 2)

        # ── TIME STOP ───────────────────────────────────────────────────────
        # Budget: bars until the next Gann cycle boundary measured from the pivot
        # the trade is anchored on. The structure's own clock, not an arbitrary
        # "hold 5 days" rule.
        anchor_bars = bars_pl if uptrend else bars_ph
        time_budget, cycle_target = _time_budget(anchor_bars)

        # Feasibility: net travel scales as ADR·bars^p. The exponent depends on
        # whether direction is confirmed — a post-BOS leg drifts, a coil does not.
        # Inverting: bars_needed = (distance / ADR)^(1/p).
        directional = setup in ("bos_pullback_long", "bos_pullback_short",
                                "pivot_breakout", "pivot_breakdown")
        p = TRAVEL_EXP_TREND if directional else TRAVEL_EXP_RANGE
        bars_needed_tp1 = None
        if tp1 and adr_pct > 0:
            dist_pct = abs(tp1 - entry) / entry * 100
            bars_needed_tp1 = int(math.ceil((dist_pct / adr_pct) ** (1.0 / p)))
        time_feasible = bool(bars_needed_tp1 is not None and bars_needed_tp1 <= time_budget)

        # Half-life rule: by half the budget the trade must show ≥ 0.5R of progress.
        # A position that is flat at the midpoint of its cycle is not "still working" —
        # it is a failed setup that has not yet cost you anything. Exit while that's true.
        half_life_bars = max(1, math.ceil(time_budget / 2))
        # entry ± 0.5R, in the trade's direction
        half_life_level = round(entry + HALF_LIFE_R * abs(entry - stop) * (1 if uptrend else -1), 2) \
            if stop else None

        last_date = df.index[-1].date()
        time_stop_date  = _add_trading_days(last_date, time_budget)
        half_life_date  = _add_trading_days(last_date, half_life_bars)

        time_stop = {
            "budget_bars":      time_budget,
            "cycle_target":     cycle_target,          # which Gann cycle we're running to
            "bars_since_pivot": anchor_bars,
            "bars_needed_tp1":  bars_needed_tp1,
            "time_feasible":    time_feasible,
            "half_life_bars":   half_life_bars,
            "half_life_level":  half_life_level,       # need ≥0.5R by this bar
            "time_stop_date":   str(time_stop_date) if time_stop_date else None,
            "half_life_date":   str(half_life_date) if half_life_date else None,
            "clock_starts":     "trigger" if dist_to_entry_pct != 0 else "now",
            "rule": (f"Exit at market on {time_stop_date} if TP1 ({tp1}) unhit. "
                     f"Exit on {half_life_date} if price has not reached "
                     f"{half_life_level} (0.5R)."),
        }

        # A target above the 52w high is a bet on NEW HIGHS inside 6 days —
        # a materially different proposition from clearing ordinary overhead
        # resistance. Surface it rather than burying it in the R:R number.
        target_above_52wh = bool(target and target > hi_252)
        # Likewise below the 52w low for shorts.
        target_below_52wl = bool(target and target < lo_252 and not uptrend)

        # Relative strength must AGREE with the trade direction. A long in a
        # stock lagging the index over 63 bars is fighting the tape; breakout
        # structure alone should not qualify it.
        rs_agrees = True
        if rs_slow is not None:
            rs_agrees = (rs_slow > 0) if uptrend else (rs_slow < 0)

        # ── Composite swing score ───────────────────────────────────────────
        # R:R is a gate, not a garnish. A perfect-looking base with sub-1.5:1
        # geometry is not a trade, so it is penalised rather than merely
        # under-rewarded — otherwise structure alone floats bad setups to the top.
        score = 0.0
        if rs_slow is not None:  score += max(min(rs_slow, 30), -30) * 0.10   # leadership
        if rs_fast is not None:  score += max(min(rs_fast, 20), -20) * 0.15
        if coil is not None:     score += max(0.0, (0.60 - coil)) * 12        # tighter = better
        if quiet_base:           score += 2.0                                 # accumulation
        if rvol_d is not None:   score += min(rvol_d, 3.0) * 1.2              # expansion now
        if nr7:                  score += 1.5
        if inside_day:           score += 0.8
        if time_cycle:           score += 2.0                                 # Gann price×time
        if pct_from_52wh > -12:  score += 1.5                                 # near highs
        if setup in ("coiled_under_pivot", "pivot_breakout"): score += 2.5

        if rr is None:        score -= 3.0
        elif rr < 1.0:        score -= 6.0        # risking more than the reward
        elif rr < 1.5:        score -= 2.0        # thin edge after costs/slippage
        else:                 score += min(rr, 4.0) * 1.5

        if stop_too_wide:     score -= 3.0        # not a 5–7 day risk profile
        if setup == "chasing":            score -= 8.0   # the move is already spent
        elif setup.endswith("_extended"): score -= 2.5   # needs a pullback first
        # A BOS pullback is the highest-quality structure this engine can find:
        # trend confirmed, risk defined by the higher low, entry far from target.
        if setup in ("bos_pullback_long", "bos_pullback_short"): score += 4.0
        if st and st.get("gann_eighth") == "4/8":  score += 1.5   # the 50% decision level
        elif st and st.get("gann_eighth"):         score += 0.8   # 3/8 or 5/8
        # Time feasibility: if TP1 cannot be reached inside the cycle budget, the
        # trade is asking for a move the instrument does not make in that window.
        if not time_feasible:             score -= 5.0
        elif bars_needed_tp1 and bars_needed_tp1 <= time_budget * 0.6:
            score += 1.5                                # comfortable time margin
        if tp1_unreachable:               score -= 2.0  # stop too tight for the noise
        if not rs_agrees:                 score -= 4.0   # fighting the index
        if target_above_52wh or target_below_52wl: score -= 1.5  # needs new extremes
        score = round(score, 2)

        return {
            "symbol": symbol,
            "name": ALL_SYMBOLS.get(symbol, symbol.replace(".NS", "")),
            "close": round(close, 2),
            "day_change_pct": chg_pct,
            "setup": setup,
            "direction": "long" if uptrend else "short",
            # historical profile
            "adr_pct": adr_pct,
            "expected_move_pct": exp_move_pct,
            "turnover_cr": turnover_cr,
            # structure
            "pivot_high": round(ph, 2), "bars_since_pivot_high": bars_ph,
            "pivot_low":  round(pl, 2), "bars_since_pivot_low":  bars_pl,
            "dist_to_pivot_high_pct": dist_to_ph,
            "dist_to_pivot_low_pct":  dist_to_pl,
            "swing_midpoint": swing_mid,
            "pct_from_52w_high": pct_from_52wh,
            "pos_in_52w_range": pos_in_range,
            # contraction / volume
            "coil_ratio": coil, "nr7": nr7, "inside_day": inside_day,
            "vol_dryup": vol_dryup, "rvol_daily": rvol_d,
            # relative strength
            "rs_21d": rs_fast, "rs_63d": rs_slow,
            # gann
            "gann": {k: v for k, v in g.items() if k != "ladder"},
            "gann_ladder": g.get("ladder", []),
            "gann_time_cycle": time_cycle,
            # trade geometry
            "entry": entry, "dist_to_entry_pct": dist_to_entry_pct,
            "target": target, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
            "risk_pct": risk_pct, "stop_too_wide": stop_too_wide,
            "extension_adr": extension, "rs_agrees": rs_agrees,
            "target_above_52wh": target_above_52wh,
            "target_below_52wl": target_below_52wl,
            "quiet_base": quiet_base,
            "coil_low": round(coil_low, 2), "coil_high": round(coil_high, 2),
            # market structure (Gann swing chart)
            "structure": st,
            # time stop (Gann cycle exit)
            "time_stop": time_stop,
            "time_feasible": time_feasible,
            "swing_score": score,
        }
    except Exception:
        return None


# ── Job runner ────────────────────────────────────────────────────────────────

def _run_scan_thread(job_id: str, symbols: list[str]) -> None:
    with _lock:
        _jobs[job_id]["status"] = "running"

    bench = _fetch_benchmark()
    b_fast = _bench_return(bench, RS_FAST)
    b_slow = _bench_return(bench, RS_SLOW)

    results: list[dict] = []
    done, total = 0, len(symbols)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_check_swing, s, b_fast, b_slow): s for s in symbols}
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                results.append(r)
            with _lock:
                _jobs[job_id]["progress"] = done
                _jobs[job_id]["total"]    = total

    ranked = sorted(results, key=lambda r: r["swing_score"], reverse=True)
    VALID = ("bos_pullback_long", "bos_pullback_short",
             "coiled_under_pivot", "coiled", "pivot_breakout", "pivot_breakdown")
    payload = {
        "candidates": ranked[:30],
        # setups that clear EVERY gate: geometry, freshness, trend agreement, TIME
        "tradeable":  [r for r in ranked
                       if r["rr"] and r["rr"] >= 1.5 and not r["stop_too_wide"]
                       and r["setup"] in VALID and r["rs_agrees"]
                       and r["time_feasible"]][:25],
        # right structure, right geometry — but TP1 is not reachable in the cycle
        "no_time":    [r for r in ranked
                       if r["setup"] in VALID and r["rr"] and r["rr"] >= 1.5
                       and not r["time_feasible"]][:25],
        # the Nykaa pattern: BOS confirmed, price retraced into Gann's eighths
        "pullback":   [r for r in ranked
                       if r["setup"] in ("bos_pullback_long", "bos_pullback_short")][:25],
        "awaiting":   [r for r in ranked if r["setup"] == "awaiting_pullback"][:25],
        # extended / spent moves, kept visible so they can be watched for pullbacks
        "chasing":    [r for r in ranked
                       if r["setup"] in ("chasing", "breakout_extended", "breakdown_extended")][:25],
        "coiled":     [r for r in ranked if r["setup"] in ("coiled", "coiled_under_pivot")][:25],
        "breakouts":  [r for r in ranked if r["setup"] in ("pivot_breakout", "pivot_breakdown")][:25],
        "time_cycle": [r for r in ranked if r["gann_time_cycle"]][:25],
        "leaders":    sorted([r for r in ranked if r["rs_63d"] is not None],
                             key=lambda r: r["rs_63d"], reverse=True)[:25],
        "all":        ranked,
        "benchmark":  {"symbol": BENCHMARK, "ret_21d": b_fast, "ret_63d": b_slow},
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    universe = _jobs[job_id].get("universe", "nifty500")
    with _lock:
        _cache[universe] = {"ts": time.time(), "data": payload}
        _jobs[job_id]["status"]  = "done"
        _jobs[job_id]["results"] = payload
        cutoff = time.time() - 7200
        for jid in [j for j, v in _jobs.items()
                    if v.get("created", 0) < cutoff and v.get("status") == "done" and j != job_id]:
            _jobs.pop(jid, None)


# ── Public API ────────────────────────────────────────────────────────────────

def start_swing_scan(universe: str = "nifty500") -> str:
    universe = universe if universe in UNIVERSES else "nifty500"
    with _lock:
        c = _cache.get(universe)
        if c and (time.time() - c["ts"]) < CACHE_TTL:
            jid = str(uuid.uuid4())
            _jobs[jid] = {"status": "done", "progress": len(UNIVERSES[universe]),
                          "total": len(UNIVERSES[universe]), "results": c["data"],
                          "universe": universe, "created": time.time()}
            return jid

    jid = str(uuid.uuid4())
    syms = UNIVERSES[universe]
    _jobs[jid] = {"status": "pending", "progress": 0, "total": len(syms),
                  "results": None, "universe": universe, "created": time.time()}
    threading.Thread(target=_run_scan_thread, args=(jid, syms), daemon=True).start()
    return jid


def get_swing_status(job_id: str) -> dict:
    with _lock:
        j = _jobs.get(job_id)
    if not j:
        return {"status": "not_found"}
    return {"status": j["status"], "progress": j["progress"],
            "total": j["total"], "results": j["results"]}
