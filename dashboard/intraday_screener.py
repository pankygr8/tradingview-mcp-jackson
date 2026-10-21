"""
intraday_screener.py — multi-universe intraday mover screener with adaptive Gann.

Universes & their character
───────────────────────────
  NSE     — session 9:15–15:30 IST, opening-range driven, gap matters.
  CRYPTO  — BTC / ETH, 24/7, anchored on the UTC daily open, no session gap,
            range-expansion & RVOL driven.
  MCX     — Crude & Natural Gas via NYMEX proxies (CL=F / NG=F), anchored on the
            NYMEX session, EIA/EIA-storage report windows flagged as catalysts.

Nothing here is a lagging indicator. Everything is either static structure known
at the session open (prev H/L/C, Gann Sq9 ladder off the open, Gann 50% midpoint)
or coincident information (gap, relative volume, opening range, range expansion).

The one thing that adapts per instrument is the Gann Square-of-9 angular step:
it is derived from each symbol's OWN median daily range so the ladder rungs land
at a sensible spacing whether the price is 3 or 6,000,000. Pure price history in,
Gann geometry out — no smoothing, no averaging of the signal itself.

Public API is unchanged: start_intraday_scan() / get_intraday_status() /
gann_calculator(). The universe name selects the profile automatically.
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
try:
    import scan_logger
except Exception:
    scan_logger = None

# ── Config ────────────────────────────────────────────────────────────────────

CACHE_TTL   = 180          # 3 min
MAX_WORKERS = 8
# Forward-plan stop for arming = OR midpoint ± this fraction of OR width.
# Below the whole OR (the old behaviour) gave 3–4% stops that pullbacks clipped.
STOP_BUFFER_FRAC = 0.15

# Per-universe behaviour. window tuple = (sh, sm, eh, em, label, weekday|None)
#   weekday None → fires every day; 0=Mon … 6=Sun → only that weekday (catalysts).
PROFILES: dict[str, dict] = {
    "nse": {
        "tz": "Asia/Kolkata", "tz_label": "IST",
        "session_mode": "daily",
        "session": (9, 15, 15, 30),      # a real bell; calendar date == session
        "proxy": False,
        "or_bars": 3, "rvol_lookback": 10,
        "rungs_per_day": 6, "rung_floor": 0.25, "rung_ceil": 1.00,
        "deg_min": 3.0, "deg_max": 140.0,
        "w": {"rvol": 2.0, "gap": 1.0, "range": 2.0, "or": 2.0},
        "windows": [
            (9, 15, 9, 45, "initial_balance", None),
            (10, 35, 10, 55, "mid_morning",    None),
            (12, 20, 12, 40, "midday",         None),
            (13, 35, 13, 55, "early_afternoon",None),
            (14, 20, 14, 40, "closing_drive",  None),
        ],
    },
    "crypto": {
        "tz": "UTC", "tz_label": "UTC",
        # Crypto has no open, no close, no opening range. Forcing a UTC calendar
        # day onto it anchors everything on the 00:00 bar — an arbitrary midnight
        # that gives a ~0% gap and a meaningless Sq9 anchor. Instead we work in a
        # ROLLING frame: the anchor and the reference range are always the last N
        # hours relative to NOW, so nothing depends on a fictional session start.
        "session_mode": "rolling",
        "roll_anchor_hrs": 24,      # Sq9 anchored on the price 24h ago
        "roll_range_hrs": 4,        # "opening range" = the recent 4h high/low
        "roll_lookback_hrs": 240,   # 10 days of history for RVOL / ADR baselines
        "proxy": False,
        "rvol_lookback": 10,
        "rungs_per_day": 6, "rung_floor": 0.25, "rung_ceil": 1.30,
        "deg_min": 1.0, "deg_max": 180.0,
        # no gap on a 24/7 tape; range/RVOL/structure carry the score
        "w": {"rvol": 2.0, "gap": 0.0, "range": 2.5, "or": 1.0},
        "windows": [
            (0, 0, 0, 30, "utc_open",  None),   # daily open — Gann anchor rotation
            (7, 45, 8, 15, "london",   None),
            (13, 15, 13, 45, "us_open",None),   # US equity open — crypto reacts
            (19, 45, 20, 15, "us_close",None),
        ],
    },
    "mcx": {
        "tz": "America/New_York", "tz_label": "ET",
        # CL/NG trade nearly around the clock on Globex, so the ET CALENDAR DAY
        # starts at midnight — the deadest half hour of the contract. Anchoring the
        # Sq9 grid and the opening range there is meaningless: it produced a 0.1%
        # "opening range" and extensions of 27x. Anchor on the liquid pit session.
        "session": (9, 0, 14, 30),
        # CL=F / NG=F ARE the traded instruments here (NYMEX front-month), not a
        # proxy for anything. The levels below are the levels you would trade.
        "proxy": False,
        "or_bars": 6, "rvol_lookback": 8,
        "rungs_per_day": 6, "rung_floor": 0.20, "rung_ceil": 1.10,
        "deg_min": 1.0, "deg_max": 180.0,
        "w": {"rvol": 2.0, "gap": 1.2, "range": 2.0, "or": 1.5},
        "windows": [
            (9, 0, 9, 20, "session_open",   None),
            (10, 25, 10, 45, "eia_crude",   2),   # Wed 10:30 ET petroleum status
            (10, 25, 10, 45, "eia_natgas",  3),   # Thu 10:30 ET natural-gas storage
            (14, 20, 14, 40, "settlement_drive", None),
        ],
    },
}


def _profile_for(universe: str) -> tuple[str, dict]:
    if universe == "crypto":
        return "crypto", PROFILES["crypto"]
    if universe == "mcx":
        return "mcx", PROFILES["mcx"]
    return "nse", PROFILES["nse"]


# screener.py labels these "MCX Crude Oil" / "MCX Natural Gas" because they were
# once used as Indian proxies. They are NYMEX front-month contracts.
NAME_OVERRIDES = {
    "CL=F": "NYMEX WTI Crude (CL)",
    "NG=F": "NYMEX Natural Gas (NG)",
}

_cache: dict[str, dict] = {}
_jobs:  dict[str, dict] = {}
_lock = threading.Lock()


# ── Gann Square of 9 ─────────────────────────────────────────────────────────

def _pick_degree_step(price: float, target_rung_pct: float, prof: dict) -> float:
    """
    Choose the Sq9 angular step so one rung ≈ target_rung_pct of price.

    Derivation: one 360° rotation = +2.0 on the square root, so a step of `s`
    on the root moves price by ≈ 2·√price·s. Setting that equal to
    (target_pct/100)·price and solving gives  degrees ≈ 0.9 · target_pct · √price.
    Clamped to the profile's [deg_min, deg_max] so it never degenerates.
    """
    if price <= 0:
        return 45.0
    deg = 0.9 * target_rung_pct * math.sqrt(price)
    return max(prof["deg_min"], min(prof["deg_max"], deg))


def gann_sq9_ladder(anchor_price: float, degree_step: float, n_side: int = 8) -> list[float]:
    """True Square-of-9 ladder: (√anchor ± i·step)² for i in 1..n_side each side."""
    if anchor_price <= 0:
        return []
    root = math.sqrt(anchor_price)
    s = degree_step / 180.0            # 180° = 1.0 on the root
    levels: set[float] = {round(anchor_price, 2)}
    for i in range(1, n_side + 1):
        levels.add(round((root + i * s) ** 2, 2))
        if root - i * s > 0:
            levels.add(round((root - i * s) ** 2, 2))
    return sorted(levels)


def gann_ladder_covering(anchor_price: float, degree_step: float,
                         lo: float, hi: float) -> list[float]:
    """
    Sq9 ladder anchored on the open but EXTENDED until it brackets [lo, hi].

    A fixed ±8 rungs around the open is not enough. A stock that runs 6% away
    from its open falls straight out of the bottom of the ladder: no support
    rungs exist, `nearest_support` is None, and no R:R can be computed at all.
    The anchor never moves — the grid just has to reach wherever price went.
    """
    if anchor_price <= 0 or hi < lo:
        return []
    root = math.sqrt(anchor_price)
    s = degree_step / 180.0
    if s <= 0:
        return []
    n_hi = int(math.ceil(abs(math.sqrt(max(hi, anchor_price)) - root) / s)) + 3
    n_lo = int(math.ceil(abs(root - math.sqrt(max(lo, 0.01))) / s)) + 3
    n = min(max(8, n_hi, n_lo), 200)
    return gann_sq9_ladder(anchor_price, degree_step, n_side=n)


def gann_trade_levels(anchor_price: float, current_price: float,
                      degree_step: float, band_lo: float | None = None,
                      band_hi: float | None = None) -> dict:
    """
    Sq9 levels bracketing the CURRENT price.

    The classic Gann calculator quotes buy-above / sell-below off the day's open.
    That is only useful while price is still near the open. Once price has
    travelled, an anchor-relative "sell below" can sit far ABOVE the market and
    means nothing. Triggers here are always relative to live price; the open
    remains the anchor of the grid, not the source of the triggers.
    """
    lo = min(x for x in (band_lo, current_price, anchor_price) if x)
    hi = max(x for x in (band_hi, current_price, anchor_price) if x)
    ladder = gann_ladder_covering(anchor_price, degree_step, lo * 0.985, hi * 1.015)
    if not ladder:
        return {}
    res = [l for l in ladder if l > current_price]
    sup = [l for l in ladder if l < current_price]
    return {
        "anchor":       round(anchor_price, 2),
        "degree_step":  round(degree_step, 2),
        # live, price-relative triggers
        "resistance":   res[:3],
        "support":      list(reversed(sup[-3:])),
        "nearest_resistance": res[0]  if res else None,
        "nearest_support":    sup[-1] if sup else None,
        "dist_to_res_pct": round((res[0] - current_price) / current_price * 100, 2) if res else None,
        "dist_to_sup_pct": round((current_price - sup[-1]) / current_price * 100, 2) if sup else None,
        "rung_pct": round((res[0] - sup[-1]) / current_price * 100, 3) if (res and sup) else None,
        "ladder": ladder,
    }


def intraday_plan(gann: dict, price: float, or_high: float, or_low: float,
                  or_state: str, adr_pct: float, prof: dict,
                  bars_elapsed: int = 0, bars_total: int = 75) -> dict:
    """
    Turn the Sq9 grid into an actionable intraday trade.

    Direction comes from the opening range — the only non-lagging intraday
    structure available before noon. Risk is defined by the OR boundary, not by
    an adjacent rung: rungs are ~0.3% apart, and a 0.3% stop on an NSE equity is
    inside the noise and inside the cost of the round trip.

    Targets are 1R and 2R, snapped UP to the next Sq9 rung so every exit lands on
    a Gann level. NOTE: because TP1 *is* 1R, its reward:risk is ≈1.0 by
    construction — that number is a design choice, not evidence of edge. The
    meaningful filter is FEASIBILITY: can price still travel to TP1 in the hours
    that remain? Expected remaining travel ≈ ADR · √(fraction of session left).
    """
    res, sup = gann.get("nearest_resistance"), gann.get("nearest_support")
    if not res or not sup or or_high <= or_low:
        return {"bias": "none"}

    or_range_pct = (or_high - or_low) / price * 100

    # An opening range narrower than a quarter of a typical day is not a range —
    # it is a few ticks of dead time. Any bias derived from breaking it is noise,
    # and the extension metric (distance / OR width) explodes toward infinity.
    if or_range_pct < 0.25 * adr_pct:
        return {"bias": "none", "or_unreliable": True,
                "or_range_pct": round(or_range_pct, 3),
                "note": "opening range too narrow to define a break"}

    if or_state == "above":
        bias, entry = "long", price
        stop = max([l for l in gann["ladder"] if l < or_low], default=or_low)
    elif or_state == "below":
        bias, entry = "short", price
        stop = min([l for l in gann["ladder"] if l > or_high], default=or_high)
    else:
        return {"bias": "none", "note": "inside opening range — no directional edge"}

    risk = abs(entry - stop)
    if risk <= 0:
        return {"bias": "none"}
    risk_pct = risk / entry * 100

    def _snap(mult: float):
        lvl = entry + mult * risk if bias == "long" else entry - mult * risk
        c = [l for l in gann["ladder"] if l >= lvl] if bias == "long" \
            else [l for l in gann["ladder"] if l <= lvl]
        return (c[0] if bias == "long" else c[-1]) if c else round(lvl, 2)

    tp1, tp2 = _snap(1.0), _snap(2.0)
    rr_tp1 = round(abs(tp1 - entry) / risk, 2)
    rr_tp2 = round(abs(tp2 - entry) / risk, 2)

    # ── Feasibility: is TP1 reachable in the window that remains? ──────────
    # Daily instruments: the window shrinks as the session ends (√ of fraction
    # left). Rolling instruments (crypto) never close, so use a FIXED forward
    # horizon — expected travel over the next `bars_total` bars — instead of a
    # shrinking session fraction.
    if prof.get("session_mode") == "rolling":
        frac_left = 1.0
        exp_remaining_pct = adr_pct * math.sqrt(min(bars_total, 48) / (24 * 12))
    else:
        frac_left = max(0.0, min(1.0, 1.0 - bars_elapsed / max(bars_total, 1)))
        exp_remaining_pct = adr_pct * math.sqrt(frac_left)
    dist_tp1_pct = abs(tp1 - entry) / entry * 100
    tp1_feasible = bool(exp_remaining_pct > 0 and dist_tp1_pct <= 0.70 * exp_remaining_pct)

    # EXTENSION: how far beyond the opening range has price already run, in units
    # of the OR's own width. Past ~1.2 the move is largely spent; entering there
    # is chasing — the same disease the swing engine had.
    ext = (price - or_high) / (or_high - or_low) if bias == "long" \
        else (or_low - price) / (or_high - or_low)
    ext = round(max(ext, 0.0), 2)

    # Data is delayed, so a level price blew through 20 minutes ago is not a
    # level — it is a memory.
    if ext <= 0.35:
        status = "actionable"          # sitting on the break
    elif ext <= 1.2:
        status = "late"                # entering mid-move; tighten expectations
    else:
        status = "missed"              # spent; watch for a retest of the OR

    return {
        "bias": bias, "entry": round(entry, 2), "stop": round(stop, 2),
        "tp1": tp1, "tp2": tp2, "rr_tp1": rr_tp1, "rr_tp2": rr_tp2,
        "risk_pct": round(risk_pct, 2),
        "or_range_pct": round(or_range_pct, 2),
        "extension_or": ext, "status": status,
        "dist_tp1_pct": round(dist_tp1_pct, 2),
        "exp_remaining_pct": round(exp_remaining_pct, 2),
        "tp1_feasible": tp1_feasible,
        "session_frac_left": round(frac_left, 2),
        "risk_too_tight": bool(risk_pct < 0.15),
        "risk_too_wide":  bool(risk_pct > max(1.5, adr_pct * 0.6)),
    }


def active_time_window(prof: dict) -> str | None:
    """Label of the profile's reversal/catalyst window we're currently inside, else None."""
    ts = pd.Timestamp.now(tz=prof["tz"])
    mins, wd = ts.hour * 60 + ts.minute, ts.weekday()
    for sh, sm, eh, em, label, wday in prof["windows"]:
        if wday is not None and wday != wd:
            continue
        if sh * 60 + sm <= mins <= eh * 60 + em:
            return label
    return None


# ── Session splitting (tz-aware, works for any universe) ─────────────────────

def _split_sessions(df: pd.DataFrame, prof: dict):
    """
    (sorted dates, {date: session bars}) with the index converted to the profile tz.

    When the profile declares a `session` window, bars outside it are DROPPED before
    grouping. Without this, a nearly-24h futures contract has its "open" set by the
    midnight bar and its opening range measured across the thinnest ticks of the day.
    """
    local = df.tz_convert(prof["tz"])
    sess = prof.get("session")
    if sess:
        sh, sm, eh, em = sess
        mins = local.index.hour * 60 + local.index.minute
        local = local[(mins >= sh * 60 + sm) & (mins <= eh * 60 + em)]
        if local.empty:
            return [], {}
    day_of = local.index.date
    dates = sorted(set(day_of))
    groups = {d: local[day_of == d] for d in dates}
    # a session must have enough bars to define an opening range at all
    need = prof.get("or_bars", 3) + 1
    dates = [d for d in dates if len(groups[d]) >= need]
    return dates, {d: groups[d] for d in dates}


# ── Per-symbol intraday analysis ─────────────────────────────────────────────

def _check_rolling(symbol: str, prof: dict) -> dict | None:
    """
    Analysis for 24/7 instruments (crypto). Everything is NOW-RELATIVE:
      • anchor   = price roll_anchor_hrs ago (Sq9 grid origin), not a midnight open
      • "OR"     = high/low of the last roll_range_hrs (the recent balance area)
      • gap      = omitted; there is no session boundary to gap across
      • RVOL/ADR = rolling baselines over roll_lookback_hrs
    The break of the recent range gives direction; the plan/geometry reused
    from the daily path is identical once those inputs are defined.
    """
    try:
        df = yf.Ticker(symbol).history(period="15d", interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df.index.tz is None or len(df) < 60:
            return None
        df = df.tz_convert(prof["tz"])
        bar_min = 5
        per_hr = 60 // bar_min

        now_price = float(df["Close"].iloc[-1])
        n_anchor = int(prof["roll_anchor_hrs"] * per_hr)
        n_range  = int(prof["roll_range_hrs"] * per_hr)
        n_look   = int(prof["roll_lookback_hrs"] * per_hr)

        if len(df) < n_range + 6:
            return None

        # anchor: price roll_anchor_hrs ago (fallback to earliest available)
        anchor_price = float(df["Close"].iloc[-n_anchor]) if len(df) > n_anchor \
            else float(df["Close"].iloc[0])
        ref_close = anchor_price                       # "prev close" analogue
        chg_pct = round((now_price - anchor_price) / anchor_price * 100, 2)

        # rolling "opening range" = recent N-hour balance area
        recent = df.tail(n_range)
        or_high = float(recent["High"].max())
        or_low  = float(recent["Low"].min())
        # but current price should sit at the EDGE for a break, so measure the
        # range EXCLUDING the last few bars, then see if price broke it
        formed = df.iloc[-(n_range + 6):-3]
        rr_high = float(formed["High"].max())
        rr_low  = float(formed["Low"].min())
        or_state = ("above" if now_price > rr_high
                    else "below" if now_price < rr_low else "inside")

        # rolling ADR% and range expansion, over 24h blocks
        look = df.tail(n_look)
        blocks = [look.iloc[i:i + int(24 * per_hr)]
                  for i in range(0, len(look), int(24 * per_hr))]
        block_ranges = [(b["High"].max() - b["Low"].min()) / b["Close"].iloc[-1] * 100
                        for b in blocks if len(b) > per_hr and b["Close"].iloc[-1] > 0]
        adr_pct = round(statistics.median(block_ranges), 2) if block_ranges else 2.0
        cur_block_range = (df.tail(int(24 * per_hr))["High"].max()
                           - df.tail(int(24 * per_hr))["Low"].min()) / now_price * 100
        range_exp = round(cur_block_range / adr_pct, 2) if adr_pct > 0 else None

        # rolling RVOL: last roll_range_hrs volume vs same-length blocks' median
        rvol = None
        if "Volume" in df.columns:
            recent_v = float(df.tail(n_range)["Volume"].sum())
            hist = df.iloc[:-n_range]
            sums = [float(hist["Volume"].iloc[i:i + n_range].sum())
                    for i in range(0, max(len(hist) - n_range, 0), n_range)]
            if sums and statistics.median(sums) > 0:
                rvol = round(recent_v / statistics.median(sums), 2)

        # Gann grid anchored on the rolling anchor price
        target_rung = max(prof["rung_floor"],
                          min(prof["rung_ceil"], adr_pct / prof["rungs_per_day"]))
        deg = _pick_degree_step(anchor_price, target_rung, prof)
        band_lo = float(df.tail(n_anchor if len(df) > n_anchor else len(df))["Low"].min())
        band_hi = float(df.tail(n_anchor if len(df) > n_anchor else len(df))["High"].max())
        gann = gann_trade_levels(anchor_price, now_price, deg, band_lo=band_lo, band_hi=band_hi)

        # plan: crypto trades continuously, so "session left" doesn't apply.
        # Feasibility uses a fixed forward window instead (the roll_range horizon).
        plan = intraday_plan(gann, now_price, rr_high, rr_low, or_state, adr_pct, prof,
                             bars_elapsed=0, bars_total=n_range)

        window = active_time_window(prof)
        near = None
        for lvl in (gann.get("nearest_support"), gann.get("nearest_resistance")):
            if lvl and abs(now_price - lvl) / now_price * 100 <= 0.25:
                near = lvl
        confluence = bool(near and window)

        trending = bool(range_exp and range_exp >= 1.0)
        w = prof["w"]
        score = 0.0
        if rvol:      score += w["rvol"] * min(rvol, 5.0)
        if range_exp: score += w["range"] * min(range_exp, 3.0)
        if or_state != "inside": score += w["or"]
        if not trending:                     score -= 8.0
        if plan.get("status") == "missed":   score -= 6.0
        elif plan.get("status") == "late":   score -= 2.0
        if plan.get("bias", "none") == "none":   score -= 4.0
        elif not plan.get("tp1_feasible"):        score -= 5.0
        else:                                     score += 3.0
        if plan.get("risk_too_wide") or plan.get("risk_too_tight"): score -= 2.0
        if confluence:                       score += 2.0
        score = round(score, 2)

        last_bar = df.index[-1]
        stale_min = int((pd.Timestamp.now(tz=prof["tz"]) - last_bar).total_seconds() // 60)

        return {
            "symbol": symbol, "name": NAME_OVERRIDES.get(symbol, ALL_SYMBOLS.get(symbol, symbol)),
            "current_price": round(now_price, 2),
            "day_open": round(anchor_price, 2),        # = rolling anchor
            "gap_pct": None,                           # no session boundary
            "day_change_pct": chg_pct,                 # change over the anchor window
            "change_from_open_pct": chg_pct,
            "prev_high": round(rr_high, 2), "prev_low": round(rr_low, 2),
            "prev_close": round(ref_close, 2),
            "gann_midpoint": round((rr_high + rr_low) / 2, 2),
            "above_gann_mid": now_price >= (rr_high + rr_low) / 2,
            "or_high": round(rr_high, 2), "or_low": round(rr_low, 2),
            "or_state": or_state,
            "rvol": rvol, "range_expansion": range_exp, "trending": trending,
            "rung_pct": round(target_rung, 3), "mover_score": score,
            "gann": {k: v for k, v in gann.items() if k != "ladder"},
            "gann_ladder": gann.get("ladder", []),
            "plan": plan,
            "arming": {"armed": False},
            "time_window": window, "confluence": confluence,
            "last_bar": str(last_bar), "stale_minutes": stale_min,
            "proxy": False,
            "rolling": True,
            "anchor_hrs": prof["roll_anchor_hrs"],
            "range_hrs": prof["roll_range_hrs"],
        }
    except Exception:
        return None


def _check_intraday(symbol: str, prof: dict) -> dict | None:
    if prof.get("session_mode") == "rolling":
        return _check_rolling(symbol, prof)
    try:
        df = yf.Ticker(symbol).history(period="15d", interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df.empty or df.index.tz is None:
            return None

        dates, groups = _split_sessions(df, prof)
        if len(dates) < 3:
            return None
        today = groups[dates[-1]]
        prev  = groups[dates[-2]]
        hist_ds = dates[:-1][-prof["rvol_lookback"]:]
        if today.empty or prev.empty:
            return None

        # ── Static structure known at the session open ──────────────────────
        day_open = float(today["Open"].iloc[0])
        pdh = float(prev["High"].max())
        pdl = float(prev["Low"].min())
        pdc = float(prev["Close"].iloc[-1])
        gann_mid = round((pdh + pdl) / 2, 2)

        current  = float(today["Close"].iloc[-1])
        gap_pct  = round((day_open - pdc) / pdc * 100, 2)
        chg_pct  = round((current - pdc) / pdc * 100, 2)
        open_chg = round((current - day_open) / day_open * 100, 2)

        # ── Opening range ───────────────────────────────────────────────────
        orb = today.head(prof["or_bars"])
        or_high = float(orb["High"].max())
        or_low  = float(orb["Low"].min())
        or_state = ("above" if current > or_high
                    else "below" if current < or_low else "inside")

        # ── RVOL — cumulative volume vs same-time-of-day baseline ───────────
        rvol = None
        if "Volume" in df.columns and float(today["Volume"].sum()) > 0:
            n = len(today)
            base = [float(groups[d]["Volume"].iloc[:n].sum())
                    for d in hist_ds if len(groups[d]) >= n]
            if base:
                avg = sum(base) / len(base)
                if avg > 0:
                    rvol = round(float(today["Volume"].sum()) / avg, 2)

        # ── Realized daily-range %, used both for range-exp and Gann calib ──
        range_pcts, ranges_abs = [], []
        for d in hist_ds:
            g = groups[d]
            hi, lo, op = float(g["High"].max()), float(g["Low"].min()), float(g["Open"].iloc[0])
            ranges_abs.append(hi - lo)
            if op > 0:
                range_pcts.append((hi - lo) / op * 100)
        avg_range = sum(ranges_abs) / len(ranges_abs) if ranges_abs else 0.0
        today_range = float(today["High"].max() - today["Low"].min())
        range_exp = round(today_range / avg_range, 2) if avg_range > 0 else None

        # ── Adaptive Gann calibration from this symbol's own history ────────
        med_range_pct = statistics.median(range_pcts) if range_pcts else 1.0
        target_rung = max(prof["rung_floor"],
                          min(prof["rung_ceil"], med_range_pct / prof["rungs_per_day"]))
        deg = _pick_degree_step(day_open, target_rung, prof)
        # Feed the ladder the band price has ACTUALLY traded today, so the grid
        # always brackets current price even after a 6% run away from the open.
        day_hi = float(today["High"].max())
        day_lo = float(today["Low"].min())
        gann = gann_trade_levels(day_open, current, deg, band_lo=day_lo, band_hi=day_hi)

        # ── Actionable trade plan with real R:R ─────────────────────────────
        adr_day = med_range_pct
        bars_total = max(len(groups[d]) for d in hist_ds) if hist_ds else 75
        plan = intraday_plan(gann, current, or_high, or_low, or_state, adr_day, prof,
                             bars_elapsed=len(today), bars_total=bars_total)

        # ── ARMING: not broken out yet, but pressed against a boundary ─────
        # Alerting on `actionable` is reactive — by definition the break already
        # happened, and on delayed data it happened ~15 minutes ago. To be early
        # you must arm BEFORE the break: identify names coiled against the OR
        # boundary with real volume, and place the alert AT the boundary so the
        # live feed (TradingView) fires it, not this screener's stale clock.
        or_w = or_high - or_low
        arming = {"armed": False}
        # Arming means COILED INSIDE the range, pressed against a boundary it has
        # not yet broken. If price is already outside the OR (current > or_high or
        # < or_low), the break has happened — that is a missed/late move, never an
        # arm. The old code let d_hi go negative when price was above or_high, which
        # armed longs into moves that had already run. Guard both ends explicitly.
        inside_or = or_low <= current <= or_high
        if or_w > 0 and or_state == "inside" and inside_or and range_exp and rvol:
            d_hi = (or_high - current) / or_w      # 0 = pinned to the top edge
            d_lo = (current - or_low) / or_w       # 0 = pinned to the bottom edge
            near_side = "high" if d_hi <= d_lo else "low"
            near_dist = min(d_hi, d_lo)
            # coiled = within the top/bottom quarter, still inside, with volume
            armed = bool(0.0 <= near_dist <= 0.25 and rvol >= 1.5 and range_exp >= 0.8)
            trigger = round(or_high if near_side == "high" else or_low, 2)
            direction = "long" if near_side == "high" else "short"

            # FORWARD PLAN: the trade that WOULD apply if price breaks the trigger.
            #
            # Two fixes from the day the arming bucket went 0-for-12:
            #  1. The stop used to sit below the WHOLE opening range — a 3–4% stop
            #     that a normal post-break pullback clipped every time. On a break
            #     of the OR high, invalidation is a return INTO the range, not all
            #     the way through it. Stop at the OR midpoint (buffered, snapped to
            #     a rung) — the level whose loss says the break failed.
            #  2. Feasibility was never checked: an arm could carry a TP1 the day
            #     couldn't reach. Apply the same tp1-reachability test the
            #     actionable plan uses, so an infeasible arm is not armed.
            fwd_entry = trigger
            or_mid = (or_high + or_low) / 2
            if direction == "long":
                raw_stop = or_mid - STOP_BUFFER_FRAC * or_w
                fwd_stop = max([l for l in gann["ladder"] if l < raw_stop], default=raw_stop)
            else:
                raw_stop = or_mid + STOP_BUFFER_FRAC * or_w
                fwd_stop = min([l for l in gann["ladder"] if l > raw_stop], default=raw_stop)
            fwd_risk = abs(fwd_entry - fwd_stop)

            def _fwd_snap(mult):
                lvl = fwd_entry + mult * fwd_risk if direction == "long" else fwd_entry - mult * fwd_risk
                c = [l for l in gann["ladder"] if l >= lvl] if direction == "long" \
                    else [l for l in gann["ladder"] if l <= lvl]
                return (c[0] if direction == "long" else c[-1]) if c else round(lvl, 2)
            fwd_tp1 = _fwd_snap(1.0) if fwd_risk > 0 else None
            fwd_tp2 = _fwd_snap(2.0) if fwd_risk > 0 else None

            # feasibility: can TP1 be reached in the session that remains?
            frac_left = max(0.0, 1.0 - len(today) / max(bars_total, 1))
            exp_remaining = adr_day * math.sqrt(frac_left)
            fwd_dist_tp1 = abs(fwd_tp1 - fwd_entry) / fwd_entry * 100 if fwd_tp1 else 999
            fwd_feasible = bool(exp_remaining > 0 and fwd_dist_tp1 <= 0.70 * exp_remaining)
            fwd_risk_pct = fwd_risk / fwd_entry * 100 if fwd_entry else 0
            # too-wide forward stop disqualifies the arm as well
            fwd_stop_ok = fwd_risk_pct <= max(1.0, adr_day * 0.5)

            # an arm is only "armed" if the projected trade is actually takeable
            armed = bool(armed and fwd_feasible and fwd_stop_ok and fwd_risk > 0)

            arming = {
                "armed": armed,
                "near_side": near_side,
                "trigger": trigger,
                "direction": direction,
                "dist_to_trigger_pct": round(abs(trigger - current) / current * 100, 2),
                "or_position": round(1 - near_dist, 2),   # 1.0 = pinned to the boundary
                # projected trade if the trigger breaks
                "fwd_entry": fwd_entry,
                "fwd_stop": round(fwd_stop, 2),
                "fwd_tp1": fwd_tp1,
                "fwd_tp2": fwd_tp2,
                "fwd_risk_pct": round(fwd_risk_pct, 2),
                "fwd_feasible": fwd_feasible,
                "fwd_stop_ok": fwd_stop_ok,
            }

        # ── Data freshness — yfinance .NS intraday runs ~15 min behind ──────
        last_bar = today.index[-1]
        now_local = pd.Timestamp.now(tz=prof["tz"])
        stale_min = int((now_local - last_bar).total_seconds() // 60)

        # ── Time / confluence ───────────────────────────────────────────────
        window = active_time_window(prof)
        near = None
        for lvl in (gann.get("nearest_support"), gann.get("nearest_resistance")):
            if lvl and abs(current - lvl) / current * 100 <= 0.25:
                near = lvl
        confluence = bool(near and window)

        # ── Composite mover score (weights vary by universe) ───────────────
        # RANGE EXPANSION IS A GATE, NOT A BONUS. Huge RVOL with a below-average
        # range is churn — heavy two-way trade going nowhere. Volume without range
        # is the classic intraday trap, and scoring it additively floats those
        # names to the top of the board. No range, no trend, no trade.
        w = prof["w"]
        trending = bool(range_exp and range_exp >= 1.0)

        score = 0.0
        if rvol:      score += w["rvol"]  * min(rvol, 5.0)
        score += w["gap"] * min(abs(gap_pct), 5.0)
        if range_exp: score += w["range"] * min(range_exp, 3.0)
        if or_state != "inside": score += w["or"]
        if not trending:                     score -= 8.0   # churn, not movement
        if plan.get("status") == "missed":   score -= 6.0   # the move is gone
        elif plan.get("status") == "late":   score -= 2.0
        if plan.get("bias","none") == "none":    score -= 4.0
        elif not plan.get("tp1_feasible"):        score -= 5.0   # no time left to reach TP1
        else:                                     score += 3.0
        if plan.get("risk_too_wide") or plan.get("risk_too_tight"): score -= 2.0
        if confluence:                       score += 2.0
        score = round(score, 2)

        return {
            "symbol":         symbol,
            "name":           NAME_OVERRIDES.get(symbol, ALL_SYMBOLS.get(symbol, symbol)),
            "current_price":  round(current, 2),
            "day_open":       round(day_open, 2),
            "gap_pct":        gap_pct,
            "day_change_pct": chg_pct,
            "change_from_open_pct": open_chg,
            "prev_high":      round(pdh, 2),
            "prev_low":       round(pdl, 2),
            "prev_close":     round(pdc, 2),
            "gann_midpoint":  gann_mid,
            "above_gann_mid": current >= gann_mid,
            "or_high":        round(or_high, 2),
            "or_low":         round(or_low, 2),
            "or_state":       or_state,
            "rvol":           rvol,
            "range_expansion":range_exp,
            "trending":       trending,
            "rung_pct":       round(target_rung, 3),
            "mover_score":    score,
            "gann":           {k: v for k, v in gann.items() if k != "ladder"},
            "gann_ladder":    gann.get("ladder", []),
            "plan":           plan,
            "arming":         arming,
            "time_window":    window,
            "confluence":     confluence,
            "last_bar":       str(last_bar),
            "stale_minutes":  stale_min,
            "proxy":          bool(prof.get("proxy")),
            "proxy_note":     prof.get("proxy_note"),
        }
    except Exception:
        return None


# ── Job runner ────────────────────────────────────────────────────────────────

def _run_scan_thread(job_id: str, symbols: list[str], prof: dict) -> None:
    with _lock:
        _jobs[job_id]["status"] = "running"

    results: list[dict] = []
    done, total = 0, len(symbols)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_intraday, s, prof): s for s in symbols}
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            with _lock:
                _jobs[job_id]["progress"] = done
                _jobs[job_id]["total"]    = total

    movers      = sorted(results, key=lambda r: r["mover_score"], reverse=True)
    confluences = [r for r in movers if r["confluence"]]
    or_breaks   = [r for r in movers if r["or_state"] != "inside"]

    armed = sorted([r for r in movers if r.get("arming", {}).get("armed")],
                   key=lambda r: r["arming"]["dist_to_trigger_pct"])
    payload = {
        # Coiled against the OR boundary, not yet broken. THESE are what you arm
        # TradingView alerts on — the break has not happened, so you can still be early.
        "arming":      armed[:25],
        # Only these are tradeable NOW: real range expansion, trigger not yet
        # blown through, R:R >= 1.5, and a sane stop distance.
        "actionable":  [r for r in movers
                        if not r.get("proxy")
                        and r["trending"] and r["plan"].get("status") == "actionable"
                        and r["plan"].get("tp1_feasible")
                        and not r["plan"].get("risk_too_wide")
                        and not r["plan"].get("risk_too_tight")][:25],
        "late":        [r for r in movers if r["plan"].get("status") == "late"][:25],
        "missed":      [r for r in movers if r["plan"].get("status") == "missed"][:25],
        "churn":       [r for r in movers if not r["trending"]][:25],
        "movers":       movers[:25],
        "confluence":   confluences,
        "or_breakouts": or_breaks[:25],
        "all":          movers,
        "scanned_at":   datetime.now(timezone.utc).isoformat(),
    }

    universe = _jobs[job_id].get("universe", "fno")
    with _lock:
        _cache[universe] = {"ts": time.time(), "data": payload}
        _jobs[job_id]["status"]  = "done"
        _jobs[job_id]["results"] = payload
        if scan_logger:
            try:
                n = scan_logger.log_scan(payload, universe)
                if n:
                    print(f"[scan_logger] +{n} new signals logged")
            except Exception as e:
                print(f"[scan_logger] skipped: {e}")
        cutoff = time.time() - 3600
        for jid in [j for j, v in _jobs.items()
                    if v.get("created", 0) < cutoff and v.get("status") == "done" and j != job_id]:
            _jobs.pop(jid, None)


# ── Public API ────────────────────────────────────────────────────────────────

def start_intraday_scan(universe: str = "fno") -> str:
    universe = universe if universe in UNIVERSES else "fno"
    _, prof = _profile_for(universe)

    with _lock:
        cached = _cache.get(universe)
        if cached and (time.time() - cached["ts"]) < CACHE_TTL:
            job_id = str(uuid.uuid4())
            _jobs[job_id] = {"status": "done", "progress": len(UNIVERSES[universe]),
                             "total": len(UNIVERSES[universe]), "results": cached["data"],
                             "universe": universe, "created": time.time()}
            return job_id

    job_id = str(uuid.uuid4())
    symbols = UNIVERSES[universe]
    _jobs[job_id] = {"status": "pending", "progress": 0, "total": len(symbols),
                     "results": None, "universe": universe, "created": time.time()}
    threading.Thread(target=_run_scan_thread, args=(job_id, symbols, prof),
                     daemon=True).start()
    return job_id


def get_intraday_status(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return {"status": "not_found"}
    return {"status": job["status"], "progress": job["progress"],
            "total": job["total"], "results": job["results"]}


def gann_calculator(price: float, universe: str = "nse",
                    degrees_step: float | None = None) -> dict:
    """
    Standalone Sq9 levels for any price. If degrees_step is None the step is
    auto-picked from the universe's default target rung %, matching the scanner.
    """
    _, prof = _profile_for(universe if universe in ("crypto", "mcx") else "nse")
    if degrees_step is None:
        target = (prof["rung_floor"] + prof["rung_ceil"]) / 2
        degrees_step = _pick_degree_step(price, target, prof)
    return gann_trade_levels(price, price, degrees_step)
