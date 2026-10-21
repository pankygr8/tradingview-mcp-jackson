"""
gann_fan_engine.py — Gann Fan halving-angle level scanner.

Construction (mirrors the TradingView chart):
──────────────────────────────────────────────
Step 1  Identify dominant swing (largest range, same pivot logic as
        signal_engine.py — pivLen=12, lbBars=200).

Step 2  Main angle θ = 45° in normalised price×time space.
        The line connects the swing origin to its opposite extreme.
        Slope_main = ±fib_rng / sw_dur  (price per bar).

Step 3  Half-angle  θ/2 = 22.5°  from same source.
        Slope_half = tan(22.5°) × fib_rng / sw_dur.

Step 4  Quarter-angle θ/4 = 11.25° from same source.
        Slope_qtr  = tan(11.25°) × fib_rng / sw_dur.

Step 5  At bar = sw_dur from source (where step-2 line hits its extreme),
        drop a vertical to the step-3 line → horizontal price Level A.

Step 6  Same vertical to the step-4 line → horizontal price Level B.

Closed-form formulas:
  From HIGH source (fan goes down-right):
      Level_A = sH − tan(22.5°) × fib_rng   ≈ sH − 0.4142 × rng
      Level_B = sH − tan(11.25°) × fib_rng   ≈ sH − 0.1989 × rng
  From LOW source (fan goes up-right):
      Level_A = sL + tan(22.5°) × fib_rng
      Level_B = sL + tan(11.25°) × fib_rng

Signal: current price is within NEAR_PCT% of any of the four levels.
  • Near a HIGH-fan level (below the high)  → potential support  → BUY zone
  • Near a LOW-fan level  (above the low)   → potential resistance → SELL zone
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import numpy as np
import yfinance as yf

from screener import ALL_SYMBOLS, UNIVERSES

# No fixed angle constants — angles are derived from each swing's own geometry

# ── Per-timeframe config ──────────────────────────────────────────────────────
TF_CONFIG: dict[str, dict] = {
    "1d": {
        "yf_period":   "2y",
        "yf_interval": "1d",
        "piv_len":     12,
        "lb_bars":     200,
        "min_rng_pct": 1.0,
        "label":       "Daily",
    },
    "15m": {
        "yf_period":   "60d",
        "yf_interval": "15m",
        "piv_len":     12,
        "lb_bars":     200,
        "min_rng_pct": 0.3,
        "label":       "15-min Intraday",
    },
}

NEAR_PCT    = 1.0   # % distance from level that counts as "near"
MAX_WORKERS = 10
CACHE_TTL   = {"1d": 3 * 3600, "15m": 15 * 60}

# ── Job / cache store ─────────────────────────────────────────────────────────
_jobs:  dict[str, dict[str, Any]] = {}
_cache: dict[str, dict[str, Any]] = {}
_lock   = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# ATR helper — used to normalise price to a scale-independent unit
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_atr(df, period: int = 14) -> float:
    """
    Average True Range over the last `period` bars.
    TR = max(H-L, |H-prev_C|, |L-prev_C|)
    """
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n     = len(df)

    if n < 2:
        return float(high[-1] - low[-1]) or 1.0

    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i]  - close[i - 1]))

    atr = float(np.mean(tr[-min(period, n):]))
    return atr if atr > 0 else 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Pivot helpers (identical to signal_engine)
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


# ═══════════════════════════════════════════════════════════════════════════════
# Level reaction / confirmation
# ═══════════════════════════════════════════════════════════════════════════════
#
# "Near" (within NEAR_PCT of a level) is a pure proximity check on the current
# bar — it says nothing about whether price actually tested the level and
# reacted. That's fine on daily, where each bar covers a full session, but on
# 15m bars price routinely drifts straight through a tightly-spaced Gann level
# for one bar and keeps going, which still counts as "near" with no bounce
# ever happening. This checks whether the level was actually touched in the
# recent lookback window and whether the bar after that touch closed away
# from it in the expected direction — the same touch → reaction → still-valid
# pattern signal_engine.py already uses for the Swing Fib signals.

REACT_LOOKBACK = 10   # bars to look back for the most recent touch of a level
REACT_WINDOW   = 3    # bars allowed for the reaction to develop after a touch
REACT_PCT      = 0.3  # min % move away from the level to count as a real reaction


def _level_reaction(df, level: float, direction: str, cur_bar: int,
                     lookback: int = REACT_LOOKBACK,
                     react_window: int = REACT_WINDOW) -> dict:
    """
    direction: "buy" (support — want price to bounce UP off the level) or
               "sell" (resistance — want price to reject DOWN off the level).

    A single bar right after the touch is a noisy test, especially on 15m
    bars — a genuine bounce can take 2-3 bars to actually clear REACT_PCT.
    So this gives the reaction a short window (react_window bars) rather than
    demanding it on the very next bar.

    Returns:
      touched_bar       — index of the most recent bar whose low/high (buy/sell)
                           came within NEAR_PCT of the level, or None if the
                           level was never actually touched in the lookback
                           window (price is "near" only because of where it
                           sits right now, not because it tested the level)
      bars_since_touch
      reaction_ok        — bool|None: did any bar within react_window bars
                            after the touch close away from the level by at
                            least REACT_PCT in the favourable direction? None
                            = the window hasn't fully played out yet (pending).
      still_valid        — bool: is the CURRENT close still on the favourable
                            side of the level (hasn't broken clean through)?
      confirmed           — bool|None: True only when touched + reacted +
                            still valid. False if it touched but the reaction
                            window elapsed with no reaction, or price has
                            since broken back through. None when there's no
                            touch yet, or the reaction window is still open
                            (pending, like the "live bar" case on the Swing
                            Fib signals).
    """
    low   = df["low"].to_numpy(dtype=float)
    high  = df["high"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)

    # Search strictly BEFORE the current bar. A level only reaches
    # near_levels because current price is already close to it, so including
    # cur_bar here would almost always "find" the touch on the live bar
    # itself and every result would come back pending — that tells us
    # nothing. We want to know whether price tested this level on some prior
    # bar and already had a chance to react by now.
    start = max(0, cur_bar - lookback)
    touched_bar = None
    for i in range(cur_bar - 1, start - 1, -1):
        px = low[i] if direction == "buy" else high[i]
        if abs(px - level) / level * 100 <= NEAR_PCT:
            touched_bar = i
            break   # most recent touch wins

    if touched_bar is None:
        return {"touched_bar": None, "bars_since_touch": None,
                "reaction_ok": None, "still_valid": None, "confirmed": None}

    def _reacted(b: int) -> bool:
        moved_pct = (float(close[b]) - level) / level * 100
        return moved_pct >= REACT_PCT if direction == "buy" else moved_pct <= -REACT_PCT

    window_end  = min(touched_bar + react_window, cur_bar)
    reaction_ok = any(_reacted(b) for b in range(touched_bar + 1, window_end + 1))

    if not reaction_ok:
        # No qualifying close yet — pending if the window hasn't fully played
        # out, otherwise a genuine miss (touched but never reacted).
        reaction_ok = None if window_end < touched_bar + react_window else False

    cur_close   = float(close[cur_bar])
    still_valid = bool(cur_close > level) if direction == "buy" else bool(cur_close < level)

    confirmed = None if reaction_ok is None else bool(reaction_ok and still_valid)

    return {
        "touched_bar":      int(touched_bar),
        "bars_since_touch": int(cur_bar - touched_bar),
        "reaction_ok":      reaction_ok,
        "still_valid":      still_valid,
        "confirmed":        confirmed,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Core: compute Gann Fan levels for one OHLCV DataFrame
# ═══════════════════════════════════════════════════════════════════════════════

def compute_gann_fan(df, cfg: dict) -> dict | None:
    """
    Returns a dict with the 4 key price levels and signal info,
    or None if no valid swing is found.
    """
    piv_len     = cfg["piv_len"]
    lb_bars     = cfg["lb_bars"]
    min_rng_pct = cfg["min_rng_pct"]

    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n     = len(df)

    if n < piv_len * 2 + 10:
        return None

    # ── Pivot arrays ──────────────────────────────────────────────────────────
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
        return None

    # ── Largest-range swing (Pine cap = 15) ───────────────────────────────────
    nH, nL   = len(ph_vals), len(pl_vals)
    cur_bar  = n - 1
    eff_min  = float(close[-1]) * min_rng_pct / 100
    best_rng = eff_min
    sH = sHB = sL = sLB = None
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
        return None

    fib_rng = float(sH - sL)
    sw_dur  = max(abs(sHB - sLB), 1)
    cur     = float(close[-1])

    # ── Compute REAL Gann angle from the swing's own geometry ─────────────────
    #
    #  Coordinate system:
    #   • Time axis  : 1 bar = 1 unit
    #   • Price axis : 1 ATR = 1 unit  (normalises across NSE/Crypto/MCX/Forex)
    #
    #  Main angle θ = arctan(fib_rng / ATR / sw_dur)
    #    → This is the actual slope of the swing in ATR-normalised space.
    #    → A 1-ATR-per-bar swing ≡ 45°; a steeper swing → larger θ; shallower → smaller θ.
    #
    #  Half-angle   = θ/2   (step 3)
    #  Quarter-angle= θ/4   (step 4)
    #
    #  Price level from HIGH fan at sw_dur bars from source:
    #    slope (price/bar) = tan(θ_half) × ATR
    #    h_level_a         = sH − tan(θ/2)  × ATR × sw_dur
    #    h_level_b         = sH − tan(θ/4)  × ATR × sw_dur
    #
    #  (Mirror for LOW fan, signs flipped)
    # ─────────────────────────────────────────────────────────────────────────
    atr = _compute_atr(df, period=14)

    # Normalised price range: how many ATR units did the swing cover?
    price_in_atr = fib_rng / atr

    # Main angle in degrees — clamped to [5°, 85°] to keep levels sensible
    angle_main = math.degrees(math.atan2(price_in_atr, sw_dur))
    angle_main = max(5.0, min(85.0, angle_main))

    half_angle    = angle_main / 2.0
    quarter_angle = angle_main / 4.0

    # Slopes back in actual price units per bar
    slope_half    = math.tan(math.radians(half_angle))    * atr
    slope_quarter = math.tan(math.radians(quarter_angle)) * atr

    # ── Four key Gann Fan price levels ────────────────────────────────────────
    # From HIGH source (fan fans DOWN-RIGHT from swing high)
    h_level_a = float(sH) - slope_half    * sw_dur   # step-5: half-angle line
    h_level_b = float(sH) - slope_quarter * sw_dur   # step-6: quarter-angle line

    # From LOW source (fan fans UP-RIGHT from swing low)
    l_level_a = float(sL) + slope_half    * sw_dur   # step-5
    l_level_b = float(sL) + slope_quarter * sw_dur   # step-6

    # ── Which source is more recent? ──────────────────────────────────────────
    recent_source = "high" if sHB > sLB else "low"

    # ── Near-level detection ──────────────────────────────────────────────────
    def _dist(lv: float) -> float:
        return abs(cur - lv) / lv * 100

    near_levels: list[dict] = []
    candidates = [
        # (label, price_level, direction_hint, source_tag)
        ("H-A", h_level_a, "buy",  "high"),   # support zone
        ("H-B", h_level_b, "buy",  "high"),   # shallower support
        ("L-A", l_level_a, "sell", "low"),    # resistance zone
        ("L-B", l_level_b, "sell", "low"),    # shallower resistance
    ]
    for lbl, lv, direction, src in candidates:
        dist = _dist(lv)
        if dist <= NEAR_PCT:
            reaction = _level_reaction(df, lv, direction, cur_bar=n - 1)
            near_levels.append({
                "label":     lbl,
                "price":     round(lv, 4),
                "dist_pct":  round(dist, 3),
                "direction": direction,
                "source":    src,
                **reaction,
            })

    # ── Overall signal ────────────────────────────────────────────────────────
    # "signal" = raw proximity only (kept for backward compatibility with the
    # existing buy/sell/both bucketing). "confirmed_signal" only counts levels
    # that were actually tested and reacted to — see _level_reaction() below.
    # Proximity alone is noisy, especially intraday: a 15m bar can sit within
    # NEAR_PCT of a level for one tick while just drifting straight through it,
    # with no bounce ever happening.
    signal: str | None = None
    if near_levels:
        dirs = {nl["direction"] for nl in near_levels}
        if dirs == {"buy"}:
            signal = "BUY"
        elif dirs == {"sell"}:
            signal = "SELL"
        else:
            signal = "BOTH"

    confirmed_dirs = {nl["direction"] for nl in near_levels if nl.get("confirmed") is True}
    confirmed_signal: str | None = None
    if confirmed_dirs == {"buy"}:
        confirmed_signal = "BUY"
    elif confirmed_dirs == {"sell"}:
        confirmed_signal = "SELL"
    elif confirmed_dirs:
        confirmed_signal = "BOTH"

    # ── Distance to nearest level (any) for sorting ───────────────────────────
    all_dists = [_dist(lv) for _, lv, _, _ in candidates]
    nearest_pct = round(min(all_dists), 3)
    nearest_level = round(
        min(candidates, key=lambda x: abs(cur - x[1]))[1], 4
    )

    return {
        # swing geometry
        "swing_high":    round(float(sH), 4),
        "swing_low":     round(float(sL), 4),
        "sw_dur":        int(sw_dur),
        "fib_rng":       round(fib_rng, 4),
        "atr":           round(atr, 4),
        "recent_source": recent_source,
        # actual computed angles (different for every stock / timeframe)
        "main_angle":    round(angle_main,    2),
        "half_angle":    round(half_angle,    2),
        "quarter_angle": round(quarter_angle, 2),
        # four derived price levels
        "h_level_a":     round(h_level_a, 4),   # high-fan, half-angle   (step 5)
        "h_level_b":     round(h_level_b, 4),   # high-fan, quarter-angle (step 6)
        "l_level_a":     round(l_level_a, 4),   # low-fan, half-angle    (step 5)
        "l_level_b":     round(l_level_b, 4),   # low-fan, quarter-angle  (step 6)
        # signal
        "current_price":   round(cur, 4),
        "near_levels":     near_levels,
        "signal":          signal,
        "confirmed_signal":confirmed_signal,
        "nearest_pct":     nearest_pct,
        "nearest_level":   nearest_level,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Per-symbol fetch + scan
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_symbol(symbol: str, name: str, timeframe: str) -> dict | None:
    cfg = TF_CONFIG[timeframe]
    try:
        tk  = yf.Ticker(symbol)
        raw = tk.history(period=cfg["yf_period"], interval=cfg["yf_interval"],
                         auto_adjust=True)
        if raw is None or raw.empty or len(raw) < cfg["piv_len"] * 2 + 10:
            return None

        raw.columns = [c.lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()

        fan = compute_gann_fan(raw, cfg)
        if fan is None:
            return None

        cur  = fan["current_price"]
        prev = float(raw["close"].iloc[-2]) if len(raw) > 1 else cur
        day_chg = round((cur - prev) / prev * 100, 2) if prev else 0.0

        return {
            "symbol":       symbol,
            "name":         name,
            "current_price": cur,
            "day_change_pct": day_chg,
            **fan,
        }

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Background job runner
# ═══════════════════════════════════════════════════════════════════════════════

def _run_scan_thread(job_id: str, symbols: dict[str, str], timeframe: str) -> None:
    sym_list = list(symbols.items())
    total    = len(sym_list)
    results  = {"buy": [], "sell": [], "both": [], "all": []}
    done     = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_scan_symbol, sym, name, timeframe): (sym, name)
                   for sym, name in sym_list}

        for fut in as_completed(futures):
            done += 1
            with _lock:
                _jobs[job_id]["progress"] = done

            row = fut.result()
            if row is None:
                continue

            results["all"].append(row)
            if row["signal"] == "BUY":
                results["buy"].append(row)
            elif row["signal"] == "SELL":
                results["sell"].append(row)
            elif row["signal"] == "BOTH":
                results["both"].append(row)

    # Sort near-level results by proximity (closest first)
    for key in ("buy", "sell", "both"):
        results[key].sort(key=lambda r: r["nearest_pct"])

    results["scanned_at"] = datetime.now(timezone.utc).isoformat()
    results["timeframe"]  = timeframe
    results["tf_label"]   = TF_CONFIG[timeframe]["label"]
    results["near_pct_threshold"] = NEAR_PCT

    cache_key = f"gann_{_jobs[job_id]['universe']}_{timeframe}"
    with _lock:
        _jobs[job_id]["status"]  = "done"
        _jobs[job_id]["results"] = results
        _cache[cache_key] = {"results": results, "ts": time.time()}


def start_scan(universe: str = "nifty50", timeframe: str = "1d") -> str:
    cache_key = f"gann_{universe}_{timeframe}"
    ttl       = CACHE_TTL.get(timeframe, 3600)

    with _lock:
        cached = _cache.get(cache_key)
        if cached and time.time() - cached["ts"] < ttl:
            new_id   = str(uuid.uuid4())
            sym_list = UNIVERSES.get(universe, [])
            _jobs[new_id] = {
                "status":    "done",
                "progress":  len(sym_list),
                "total":     len(sym_list),
                "universe":  universe,
                "timeframe": timeframe,
                "results":   cached["results"],
            }
            return new_id

    syms   = {s: ALL_SYMBOLS[s] for s in UNIVERSES.get(universe, []) if s in ALL_SYMBOLS}
    job_id = str(uuid.uuid4())

    with _lock:
        _jobs[job_id] = {
            "status":    "running",
            "progress":  0,
            "total":     len(syms),
            "universe":  universe,
            "timeframe": timeframe,
            "results":   None,
        }

    t = threading.Thread(
        target=_run_scan_thread,
        args=(job_id, syms, timeframe),
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
        "universe":  job["universe"],
        "timeframe": job["timeframe"],
        "results":   job["results"],
    }
