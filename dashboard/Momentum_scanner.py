"""
momentum_scanner.py
-------------------
Separate intraday momentum scanner: combines VOLUME with structural /
coincident parameters only. No smoothed indicators anywhere.

ARCHITECTURAL NOTE (flagged per project convention):
  Volume signals require a baseline to define "unusual". This scanner uses
  MEDIANS (order statistics) of prior-session values — never means, never
  smoothed series. Medians of raw observations are not smoothers; they are
  robust reference points. If this is still too close to the line, swap
  BASELINE_STAT to a fixed percentile or a single reference session.

Parameters combined (all structural or coincident):
  1. Time-of-day RVOL  : cum. volume vs median cum. volume at same bar index
  2. Gap structure     : open vs prev close AND vs prev high/low (breakaway vs inside)
  3. Opening range     : 15-min OR anchored at 09:15 IST; ORB needs volume on the bar
  4. Range expansion   : running day range vs median prior daily range
  5. PDH/PDL break-and-hold : break + >=1 bar holding beyond level
  6. Sq9 confluence    : proximity of price / breakout level to Gann rungs
  7. Liquidity gate    : median turnover floor

Output buckets match the intraday board taxonomy:
  actionable / arming / late / churn

Trade plan geometry (consistent with intraday_screener.py):
  stop  = OR boundary (opposite side)
  tp1   = 1R snapped to nearest Sq9 rung in trade direction
  tp2   = 2R snapped likewise
  tp1_feasible gate = |entry->tp1| <= remaining_bars * median_bar_range

Usage:
  python momentum_scanner.py --symbols RELIANCE.NS TATASTEEL.NS ...
  python momentum_scanner.py --universe nse_watchlist.txt
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, asdict, field
from datetime import time as dtime
from typing import Optional

import pandas as pd
import yfinance as yf

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

IST = "Asia/Kolkata"
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
INTERVAL = "5m"
LOOKBACK_PERIOD = "15d"          # yfinance window for 5m bars
BASELINE_SESSIONS = 10           # prior sessions used for medians
OR_BARS = 3                      # 3 x 5m = 15-minute opening range

RVOL_ARMING = 1.5
RVOL_ACTIONABLE = 2.0
RANGE_EXPANSION_MIN = 1.3
MIN_MEDIAN_TURNOVER = 5e7        # INR 5 cr median daily turnover liquidity gate
RUNG_NEAR_PCT = 0.35             # % distance to call Sq9 confluence
LATE_EXTENSION_R = 1.0           # already moved >1 OR-width beyond breakout = late
SESSION_BARS_TOTAL = 75          # 09:15-15:30 in 5m bars


# ----------------------------------------------------------------------------
# Gann Square-of-9 ladder (auto-extending, covering price)
# ----------------------------------------------------------------------------

def gann_ladder_covering(price: float, span: int = 8, step_deg: float = 45.0) -> list[float]:
    """Return Sq9 rungs surrounding `price`, auto-extended so price never
    falls outside the grid. Rungs at step_deg increments on the sqrt scale."""
    if price <= 0:
        return []
    root = math.sqrt(price)
    inc = step_deg / 180.0          # 45 deg = 0.25 on the root scale
    base = math.floor(root / inc) * inc
    rungs = []
    for k in range(-span, span + 1):
        r = base + k * inc
        if r > 0:
            rungs.append(round(r * r, 2))
    # auto-extend if price sits at an edge
    while rungs and price < rungs[0]:
        r = math.sqrt(rungs[0]) - inc
        if r <= 0:
            break
        rungs.insert(0, round(r * r, 2))
    while rungs and price > rungs[-1]:
        r = math.sqrt(rungs[-1]) + inc
        rungs.append(round(r * r, 2))
    return rungs


def nearest_rung(price: float, rungs: list[float]) -> tuple[float, float]:
    """Return (rung, pct_distance)."""
    if not rungs:
        return (price, 0.0)
    rung = min(rungs, key=lambda r: abs(r - price))
    return rung, abs(rung - price) / price * 100.0


def _rung_step(rungs: list[float]) -> float:
    """Root-scale spacing of the ladder (0.25 for the default 45-degree grid)."""
    if len(rungs) >= 2:
        return round(math.sqrt(rungs[1]) - math.sqrt(rungs[0]), 4)  # undo 2-dp rounding of rungs
    return 45.0 / 180.0


def snap_to_rung(level: float, rungs: list[float], direction: str) -> float:
    """Snap a raw target to the nearest rung at-or-beyond in trade direction.

    The ladder only spans +/-8 rungs around the CURRENT price. When the trigger
    is far from price (an arming name), entry + 1R can lie beyond the last rung.
    The old fallback returned the outermost rung, which can sit BELOW a long's
    entry (or above a short's), producing a TP1 on the wrong side of the trade.
    Rungs are exactly (k * step)^2 on the root scale, so the next rung beyond
    any level can be computed directly instead of being clipped to the grid.
    """
    if not rungs:
        return level
    if direction == "long":
        beyond = [r for r in rungs if r >= level]
        if beyond:
            return min(beyond)
        step = _rung_step(rungs)
        return round((math.ceil(math.sqrt(level) / step) * step) ** 2, 2)
    beyond = [r for r in rungs if r <= level]
    if beyond:
        return max(beyond)
    if level <= 0:
        return level
    step = _rung_step(rungs)
    k = math.floor(math.sqrt(level) / step)
    return round((k * step) ** 2, 2) if k > 0 else round(level, 2)


def next_rung(level: float, rungs: list[float], direction: str) -> float:
    """The rung strictly beyond `level` in the trade direction (off-grid safe)."""
    step = _rung_step(rungs)
    root = math.sqrt(level)
    if direction == "long":
        return round((root + step) ** 2, 2)
    r = root - step
    return round(r * r, 2) if r > 0 else level


# ----------------------------------------------------------------------------
# Session handling (anchored, IST)
# ----------------------------------------------------------------------------

def split_sessions(df: pd.DataFrame) -> dict:
    """Group intraday bars into trading sessions anchored at 09:15 IST.
    NSE sessions never cross midnight, but we still anchor explicitly:
    bars before 09:15 (pre-open prints) are dropped, not merged backwards."""
    if df.empty:
        return {}
    if df.index.tz is None:
        df = df.tz_localize("UTC")
    df = df.tz_convert(IST)
    df = df[(df.index.time >= SESSION_OPEN) & (df.index.time <= SESSION_CLOSE)]
    return {d: g for d, g in df.groupby(df.index.date) if len(g) >= OR_BARS}


# ----------------------------------------------------------------------------
# Result container
# ----------------------------------------------------------------------------

@dataclass
class ScanResult:
    symbol: str
    price: float
    direction: str                # long / short / none
    bucket: str                   # actionable / arming / late / churn
    score: float
    rvol_tod: float               # time-of-day relative volume
    gap_pct: float
    gap_type: str                 # breakaway_up / breakaway_down / inside / none
    or_high: float
    or_low: float
    or_state: str                 # above_or / below_or / inside_or
    orb_volume_ok: bool
    range_expansion: float
    pdh_break_hold: bool
    pdl_break_hold: bool
    sq9_rung: float
    sq9_dist_pct: float
    sq9_confluence: bool
    entry: Optional[float] = None
    stop: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_tp1: Optional[float] = None
    tp1_feasible: bool = False
    bars_remaining: int = 0
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------
# Core per-symbol scan
# ----------------------------------------------------------------------------

def scan_symbol(symbol: str) -> Optional[ScanResult]:
    try:
        raw = yf.download(symbol, period=LOOKBACK_PERIOD, interval=INTERVAL,
                          progress=False, auto_adjust=False)
    except Exception as e:
        print(f"  [{symbol}] download failed: {e}", file=sys.stderr)
        return None
    if raw is None or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    sessions = split_sessions(raw)
    if len(sessions) < BASELINE_SESSIONS + 1:
        return None

    dates = sorted(sessions.keys())
    today = dates[-1]
    prior_dates = dates[-(BASELINE_SESSIONS + 1):-1]
    cur = sessions[today]
    prev = sessions[dates[-2]]

    # ---------- Liquidity gate (median turnover, prior sessions) ----------
    turnovers = [ (sessions[d]["Close"] * sessions[d]["Volume"]).sum()
                  for d in prior_dates ]
    if pd.Series(turnovers).median() < MIN_MEDIAN_TURNOVER:
        return None

    # ---------- 1. Time-of-day RVOL ----------
    n_bars = len(cur)
    cum_vol_today = float(cur["Volume"].sum())
    baseline_cums = []
    for d in prior_dates:
        s = sessions[d]
        baseline_cums.append(float(s["Volume"].iloc[:min(n_bars, len(s))].sum()))
    med_cum = pd.Series(baseline_cums).median()
    rvol_tod = cum_vol_today / med_cum if med_cum > 0 else 0.0

    # ---------- 2. Gap structure ----------
    prev_close = float(prev["Close"].iloc[-1])
    prev_high = float(prev["High"].max())
    prev_low = float(prev["Low"].min())
    today_open = float(cur["Open"].iloc[0])
    gap_pct = (today_open - prev_close) / prev_close * 100.0
    if today_open > prev_high:
        gap_type = "breakaway_up"
    elif today_open < prev_low:
        gap_type = "breakaway_down"
    elif abs(gap_pct) >= 0.5:
        gap_type = "inside"
    else:
        gap_type = "none"

    # ---------- 3. Opening range (anchored) ----------
    or_slice = cur.iloc[:OR_BARS]
    or_high = float(or_slice["High"].max())
    or_low = float(or_slice["Low"].min())
    or_width = or_high - or_low
    price = float(cur["Close"].iloc[-1])
    if price > or_high:
        or_state = "above_or"
    elif price < or_low:
        or_state = "below_or"
    else:
        or_state = "inside_or"

    # ORB bar volume check: the first bar closing beyond the OR must itself
    # print above the median bar volume at that time-of-day
    orb_volume_ok = False
    post_or = cur.iloc[OR_BARS:]
    for i, (ts, bar) in enumerate(post_or.iterrows()):
        bar_idx = OR_BARS + i
        broke = bar["Close"] > or_high or bar["Close"] < or_low
        if broke:
            baseline_bar_vols = []
            for d in prior_dates:
                s = sessions[d]
                if len(s) > bar_idx:
                    baseline_bar_vols.append(float(s["Volume"].iloc[bar_idx]))
            med_bar_vol = pd.Series(baseline_bar_vols).median() if baseline_bar_vols else 0
            orb_volume_ok = med_bar_vol > 0 and float(bar["Volume"]) >= med_bar_vol
            break

    # ---------- 4. Range expansion ----------
    day_range = float(cur["High"].max() - cur["Low"].min())
    prior_ranges = [ float(sessions[d]["High"].max() - sessions[d]["Low"].min())
                     for d in prior_dates ]
    med_range = pd.Series(prior_ranges).median()
    range_expansion = day_range / med_range if med_range > 0 else 0.0

    # ---------- 5. PDH / PDL break-and-hold ----------
    def break_and_hold(level: float, above: bool) -> bool:
        closes = cur["Close"]
        beyond = closes > level if above else closes < level
        # need the breaking bar plus at least one subsequent bar holding
        idx = beyond[beyond].index
        if len(idx) < 2:
            return False
        pos = closes.index.get_loc(idx[0])
        return bool(pos + 1 < len(closes) and beyond.iloc[pos + 1])

    pdh_break_hold = break_and_hold(prev_high, above=True)
    pdl_break_hold = break_and_hold(prev_low, above=False)

    # ---------- 6. Sq9 confluence ----------
    rungs = gann_ladder_covering(price)
    sq9_rung, sq9_dist = nearest_rung(price, rungs)
    sq9_confluence = sq9_dist <= RUNG_NEAR_PCT

    # ---------- Direction ----------
    long_evidence = sum([
        or_state == "above_or",
        gap_type == "breakaway_up",
        pdh_break_hold,
    ])
    short_evidence = sum([
        or_state == "below_or",
        gap_type == "breakaway_down",
        pdl_break_hold,
    ])
    if long_evidence > short_evidence and long_evidence >= 1:
        direction = "long"
    elif short_evidence > long_evidence and short_evidence >= 1:
        direction = "short"
    else:
        direction = "none"

    # ---------- Composite score (0-100) ----------
    score = 0.0
    score += min(rvol_tod / RVOL_ACTIONABLE, 1.5) * 30          # volume: up to 45
    score += 15 if gap_type in ("breakaway_up", "breakaway_down") else 0
    score += 15 if or_state in ("above_or", "below_or") and orb_volume_ok else \
             7 if or_state in ("above_or", "below_or") else 0
    score += 10 if range_expansion >= RANGE_EXPANSION_MIN else 0
    score += 10 if (pdh_break_hold or pdl_break_hold) else 0
    score += 5 if sq9_confluence else 0
    score = round(min(score, 100.0), 1)

    # ---------- Trade plan (OR-boundary stop, rung-snapped targets) ----------
    notes = []
    entry = stop = tp1 = tp2 = rr_tp1 = None
    tp1_feasible = False
    bars_remaining = max(SESSION_BARS_TOTAL - n_bars, 0)

    if direction in ("long", "short") and or_width > 0:
        if direction == "long":
            entry = or_high
            stop = or_low
            raw_tp1 = entry + 1.0 * or_width
            raw_tp2 = entry + 2.0 * or_width
        else:
            entry = or_low
            stop = or_high
            raw_tp1 = entry - 1.0 * or_width
            raw_tp2 = entry - 2.0 * or_width
        tp1 = snap_to_rung(raw_tp1, rungs, direction)
        tp2 = snap_to_rung(raw_tp2, rungs, direction)
        if tp2 == tp1:
            # narrow OR vs rung spacing: force tp2 one rung beyond tp1
            if direction == "long":
                higher = [r for r in rungs if r > tp1]
                tp2 = min(higher) if higher else next_rung(tp1, rungs, "long")
            else:
                lower = [r for r in rungs if r < tp1]
                tp2 = max(lower) if lower else next_rung(tp1, rungs, "short")
        risk = abs(entry - stop)
        rr_tp1 = round(abs(tp1 - entry) / risk, 2) if risk > 0 else None

        # feasibility: can price cover entry->tp1 in remaining bars, given
        # the median 5m bar range of THIS session (coincident, not smoothed)
        med_bar_range = float((cur["High"] - cur["Low"]).median())
        needed = abs(tp1 - price)
        tp1_feasible = med_bar_range > 0 and needed <= bars_remaining * med_bar_range

        # extension: how far beyond breakout has price already run
        ext_r = (price - entry) / or_width if direction == "long" else (entry - price) / or_width
        if ext_r > LATE_EXTENSION_R:
            notes.append(f"extended {ext_r:.1f} OR-widths past trigger")

    # ---------- Bucket ----------
    extended = any("extended" in n for n in notes)
    if direction == "none" or rvol_tod < RVOL_ARMING:
        bucket = "churn"
    elif extended:
        bucket = "late"
    elif rvol_tod >= RVOL_ACTIONABLE and orb_volume_ok and tp1_feasible and \
            or_state in ("above_or", "below_or"):
        bucket = "actionable"
    elif rvol_tod >= RVOL_ARMING:
        bucket = "arming"
    else:
        bucket = "churn"

    return ScanResult(
        symbol=symbol, price=round(price, 2), direction=direction, bucket=bucket,
        score=score, rvol_tod=round(rvol_tod, 2), gap_pct=round(gap_pct, 2),
        gap_type=gap_type, or_high=round(or_high, 2), or_low=round(or_low, 2),
        or_state=or_state, orb_volume_ok=orb_volume_ok,
        range_expansion=round(range_expansion, 2),
        pdh_break_hold=pdh_break_hold, pdl_break_hold=pdl_break_hold,
        sq9_rung=sq9_rung, sq9_dist_pct=round(sq9_dist, 2),
        sq9_confluence=sq9_confluence,
        entry=round(entry, 2) if entry else None,
        stop=round(stop, 2) if stop else None,
        tp1=round(tp1, 2) if tp1 else None,
        tp2=round(tp2, 2) if tp2 else None,
        rr_tp1=rr_tp1, tp1_feasible=tp1_feasible,
        bars_remaining=bars_remaining, notes=notes,
    )


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Volume + structural momentum scanner")
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--universe", help="text file, one yfinance symbol per line")
    ap.add_argument("--json-out", default="momentum_scan.json")
    args = ap.parse_args()

    symbols = list(args.symbols)
    if args.universe:
        with open(args.universe) as f:
            symbols += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not symbols:
        print("No symbols. Use --symbols or --universe.")
        return

    results: list[ScanResult] = []
    for sym in symbols:
        r = scan_symbol(sym)
        if r:
            results.append(r)

    order = {"actionable": 0, "arming": 1, "late": 2, "churn": 3}
    results.sort(key=lambda r: (order.get(r.bucket, 9), -r.score))

    hdr = f"{'SYMBOL':<16}{'BUCKET':<12}{'DIR':<7}{'SCORE':<7}{'RVOL':<7}" \
          f"{'GAP%':<7}{'ORSTATE':<11}{'RNGX':<7}{'SQ9':<6}{'ENTRY':<9}" \
          f"{'STOP':<9}{'TP1':<9}{'RR':<6}{'FEAS':<5}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r.symbol:<16}{r.bucket:<12}{r.direction:<7}{r.score:<7}"
              f"{r.rvol_tod:<7}{r.gap_pct:<7}{r.or_state:<11}"
              f"{r.range_expansion:<7}{'Y' if r.sq9_confluence else '-':<6}"
              f"{str(r.entry or '-'):<9}{str(r.stop or '-'):<9}"
              f"{str(r.tp1 or '-'):<9}{str(r.rr_tp1 or '-'):<6}"
              f"{'Y' if r.tp1_feasible else '-':<5}")

    with open(args.json_out, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2, default=str)
    print(f"\nWrote {len(results)} results -> {args.json_out}")


if __name__ == "__main__":
    main()
