"""
validator.py — grade every stored signal against what price actually did.

Two lenses per signal:

1. Forward returns (no trade management)
   daily    : entry at the NEXT session's open (a scan after the close cannot
              trade the close it just saw); returns at +1/3/5/10/20 sessions,
              excess vs Nifty, and max favourable / adverse excursion.
   intraday : entry at the open of the first 5-min bar at/after the scan time;
              returns at +30/+60 min and at the close, excess vs Nifty.

2. Trade simulation in R
   native   : the scanner's own entry / stop / TP1 / time budget, when it has one
   uniform  : one identical plan for every scanner, so a scanner without its
              own stops can still be compared in R:
                daily    stop 1.5×ATR, target 3×ATR, exit after 10 bars
                intraday stop 0.25×median daily range, target 2R, exit at close

Honesty rules (same as score_signals.py)
  • only bars AFTER the signal can grade it
  • a bar that touches both stop and target counts as the stop
  • a gap through the stop exits at the open (worse than -1R is possible)
  • a trigger entry that never fills is "no_fill" — not a win, not a loss
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime

import pandas as pd

from . import config, store
from .prices import PriceCache, atr, today_ist

SESSION_END = dtime(15, 30)


# ═════════════════════════════════════════════════════════════════════════════
# Generic bar-by-bar trade simulator
# ═════════════════════════════════════════════════════════════════════════════

def simulate(bars: pd.DataFrame, side: str, entry_type: str, entry: float | None,
             stop: float, target: float | None, ref_price: float | None,
             fill_window: int | None, max_bars: int | None, market_fill: float | None = None) -> dict:
    """
    bars        : forward bars only, in order (Open/High/Low/Close)
    entry_type  : market → fill at market_fill (or first bar's open)
                  trigger→ buy-stop/sell-stop or limit at `entry`, decided by
                           which side of ref_price the entry sits
    max_bars    : bars held after the fill before a time exit (None = run out)
    Returns outcome in {win, loss, time_exit, open, no_fill, invalid}.
    """
    long = side == "long"
    sgn = 1 if long else -1
    if bars is None or bars.empty or stop is None:
        return {"outcome": "invalid"}

    fill = None
    fill_i = None
    O, H, L, C = (bars[c].to_numpy(dtype=float) for c in ("Open", "High", "Low", "Close"))
    n = len(bars)

    if entry_type == "market" or entry is None:
        fill, fill_i = (market_fill if market_fill else O[0]), 0
    else:
        # breakout trigger if the entry is beyond the price seen, else a pull-back limit
        breakout = (entry >= (ref_price or entry)) if long else (entry <= (ref_price or entry))
        last = n if fill_window is None else min(n, fill_window)
        for i in range(last):
            if long and breakout and H[i] >= entry:
                fill, fill_i = max(O[i], entry), i
            elif long and not breakout and L[i] <= entry:
                fill, fill_i = min(O[i], entry), i
            elif not long and breakout and L[i] <= entry:
                fill, fill_i = min(O[i], entry), i
            elif not long and not breakout and H[i] >= entry:
                fill, fill_i = max(O[i], entry), i
            if fill is not None:
                break
        if fill is None:
            done = fill_window is not None and n >= fill_window
            return {"outcome": "no_fill" if done else "open", "complete": done}

    risk = abs(fill - stop)
    if risk <= 0 or (long and stop >= fill) or (not long and stop <= fill):
        return {"outcome": "invalid", "reason": "stop_wrong_side", "fill": fill, "complete": True}
    if target and ((long and target <= fill) or (not long and target >= fill)):
        # e.g. Momentum_scanner snaps TP1 to the top of a ladder that does not
        # reach the target, which can put a long's TP1 BELOW its entry
        return {"outcome": "invalid", "reason": "target_wrong_side", "fill": fill, "complete": True}

    def R(px):
        return round(sgn * (px - fill) / risk, 3)

    end = n if max_bars is None else min(n, fill_i + max_bars)
    for i in range(fill_i, end):
        o, h, l = O[i], H[i], L[i]
        if i > fill_i:   # gaps only matter on bars after the fill bar
            if (long and o <= stop) or (not long and o >= stop):
                return {"outcome": "loss", "r": R(o), "fill": fill, "exit": o, "bars": i - fill_i + 1, "complete": True}
            if target and ((long and o >= target) or (not long and o <= target)):
                return {"outcome": "win", "r": R(o), "fill": fill, "exit": o, "bars": i - fill_i + 1, "complete": True}
        hit_stop = (l <= stop) if long else (h >= stop)
        hit_tgt = bool(target) and ((h >= target) if long else (l <= target))
        if hit_stop:                                 # stop-first on ambiguity
            return {"outcome": "loss", "r": R(stop), "fill": fill, "exit": stop, "bars": i - fill_i + 1, "complete": True}
        if hit_tgt:
            return {"outcome": "win", "r": R(target), "fill": fill, "exit": target, "bars": i - fill_i + 1, "complete": True}

    last_close = C[end - 1]
    complete = max_bars is not None and end - fill_i >= max_bars
    return {"outcome": "time_exit" if complete else "open", "r": R(last_close), "fill": fill,
            "exit": last_close, "bars": end - fill_i, "complete": complete}


# ═════════════════════════════════════════════════════════════════════════════
# Daily-horizon signals
# ═════════════════════════════════════════════════════════════════════════════

def grade_daily(sig, df: pd.DataFrame | None, bench: pd.DataFrame | None, cfg: dict,
                asof: date | None = None) -> tuple[str, dict]:
    if df is None or df.empty:
        return "no_data", {}
    D = pd.Timestamp(sig["session"])
    hist = df[df.index <= D]
    fwd = df[df.index > D]
    if asof is not None:
        fwd = fwd[fwd.index <= pd.Timestamp(asof)]
    if fwd.empty:
        return "pending", {}
    side = sig["side"]
    sgn = 1 if side == "long" else -1
    base = float(fwd["Open"].iloc[0])
    m: dict = {"base": base, "base_date": str(fwd.index[0].date())}

    bench_base = None
    if bench is not None and not bench.empty:
        bb = bench[bench.index >= fwd.index[0]]
        if not bb.empty and float(bb["Open"].iloc[0]) > 0:
            bench_base = float(bb["Open"].iloc[0])

    horizons = cfg["daily_horizons"]
    for h in horizons:
        if len(fwd) >= h:
            px = float(fwd["Close"].iloc[h - 1])
            r = sgn * (px / base - 1) * 100
            m[f"ret_{h}d"] = round(r, 3)
            if bench_base:
                bpx = bench[bench.index <= fwd.index[h - 1]]["Close"]
                if not bpx.empty:
                    m[f"xret_{h}d"] = round(r - sgn * (float(bpx.iloc[-1]) / bench_base - 1) * 100, 3)

    ph = cfg["primary_daily_horizon"]
    win = fwd.head(ph)
    if side == "long":
        m["mfe"] = round((float(win["High"].max()) / base - 1) * 100, 3)
        m["mae"] = round((float(win["Low"].min()) / base - 1) * 100, 3)
    else:
        m["mfe"] = round((1 - float(win["Low"].min()) / base) * 100, 3)
        m["mae"] = round((1 - float(win["High"].max()) / base) * 100, 3)

    complete = len(fwd) >= max(horizons)

    # native plan
    if sig["stop"] and sig["entry"]:
        nat = simulate(fwd, side, sig["entry_type"] or "market", sig["entry"], sig["stop"],
                       sig["tp1"], sig["ref_price"], cfg["trigger_fill_window_bars"],
                       sig["time_budget"] or cfg["uniform_daily_plan"]["max_bars"])
        m.update({f"native_{k}": v for k, v in nat.items()})
        complete = complete and nat.get("complete", nat["outcome"] in ("invalid",))

    # uniform plan
    up = cfg["uniform_daily_plan"]
    a = atr(hist.tail(30))
    if a:
        stop = base - sgn * up["stop_atr"] * a
        tgt = base + sgn * up["target_atr"] * a
        uni = simulate(fwd, side, "market", None, stop, tgt, None, None, up["max_bars"], market_fill=base)
        m.update({f"uni_{k}": v for k, v in uni.items()})
        m["atr_pct"] = round(a / base * 100, 3)
        complete = complete and uni.get("complete", False)

    return ("done" if complete else "partial"), m


# ═════════════════════════════════════════════════════════════════════════════
# Intraday-horizon signals
# ═════════════════════════════════════════════════════════════════════════════

def grade_intraday(sig, df5: pd.DataFrame | None, bench5: pd.DataFrame | None, cfg: dict,
                   now: datetime | None = None) -> tuple[str, dict]:
    sess = date.fromisoformat(sig["session"])
    if df5 is None or df5.empty:
        too_old = (today_ist() - sess).days > 55
        return ("no_data" if too_old else "pending"), {}
    day = df5[df5.index.date == sess]
    if day.empty:
        too_old = (today_ist() - sess).days > 55 or sess < today_ist()
        return ("no_data" if too_old else "pending"), {}
    ts = pd.Timestamp(sig["signal_ts"])
    ts = ts.tz_localize("Asia/Kolkata") if ts.tzinfo is None else ts.tz_convert("Asia/Kolkata")
    fwd = day[day.index >= ts]
    session_over = sess < today_ist() or (now and now.time() > SESSION_END)
    if fwd.empty:
        return ("no_data" if session_over else "pending"), {}

    side = sig["side"]
    sgn = 1 if side == "long" else -1
    base = float(fwd["Open"].iloc[0])
    m: dict = {"base": base, "base_ts": str(fwd.index[0])}

    bday = None
    if bench5 is not None and not bench5.empty:
        bday = bench5[(bench5.index.date == sess) & (bench5.index >= ts)]

    def _ret_at(minutes: int | None):
        sub = fwd if minutes is None else fwd[fwd.index < fwd.index[0] + pd.Timedelta(minutes=minutes)]
        if minutes is not None and (fwd.index[-1] - fwd.index[0]) < pd.Timedelta(minutes=minutes - 5):
            return None, None
        px = float(sub["Close"].iloc[-1])
        r = sgn * (px / base - 1) * 100
        x = None
        if bday is not None and not bday.empty:
            bsub = bday if minutes is None else bday[bday.index < bday.index[0] + pd.Timedelta(minutes=minutes)]
            if not bsub.empty and float(bsub["Open"].iloc[0]) > 0:
                x = r - sgn * (float(bsub["Close"].iloc[-1]) / float(bsub["Open"].iloc[0]) - 1) * 100
        return round(r, 3), (round(x, 3) if x is not None else None)

    for mins in cfg["intraday_horizons_min"]:
        r, x = _ret_at(mins)
        if r is not None:
            m[f"ret_{mins}m"] = r
            if x is not None:
                m[f"xret_{mins}m"] = x
    if session_over:
        r, x = _ret_at(None)
        m["ret_eod"] = r
        if x is not None:
            m["xret_eod"] = x

    if side == "long":
        m["mfe"] = round((float(fwd["High"].max()) / base - 1) * 100, 3)
        m["mae"] = round((float(fwd["Low"].min()) / base - 1) * 100, 3)
    else:
        m["mfe"] = round((1 - float(fwd["Low"].min()) / base) * 100, 3)
        m["mae"] = round((1 - float(fwd["High"].max()) / base) * 100, 3)

    if sig["stop"]:
        nat = simulate(fwd, side, sig["entry_type"] or "market", sig["entry"], sig["stop"],
                       sig["tp1"], sig["ref_price"], None, None)
        if session_over and nat["outcome"] == "open":
            nat["outcome"] = "time_exit" if nat.get("fill") else "no_fill"
        m.update({f"native_{k}": v for k, v in nat.items()})

    # uniform: stop = 0.25 × median daily range of the prior sessions in the 5m data
    prior = df5[df5.index.date < sess]
    if not prior.empty:
        g = prior.groupby(prior.index.date)
        rng = ((g["High"].max() - g["Low"].min()) / g["Close"].last()).tail(10)
        med = float(rng.median()) if len(rng) else None
        if med and med > 0:
            up = cfg["uniform_intraday_plan"]
            dist = base * med * up["stop_adr_frac"]
            uni = simulate(fwd, side, "market", None, base - sgn * dist,
                           base + sgn * dist * up["target_r"], None, None, None, market_fill=base)
            if session_over and uni["outcome"] == "open":
                uni["outcome"] = "time_exit"
            m.update({f"uni_{k}": v for k, v in uni.items()})

    return ("done" if session_over else "partial"), m


# ═════════════════════════════════════════════════════════════════════════════
# Driver
# ═════════════════════════════════════════════════════════════════════════════

def validate(con=None, cfg: dict | None = None, prices: PriceCache | None = None,
             log=print, asof: date | None = None) -> dict:
    cfg = cfg or config.load()
    con = con or store.connect()
    prices = prices or PriceCache()
    sigs = store.pending_signals(con)
    if not sigs:
        log("validate: nothing pending")
        return {"graded": 0}

    daily_syms = sorted({s["symbol"] for s in sigs if s["horizon"] == "daily"})
    if daily_syms:
        log(f"validate: fetching daily bars for {len(daily_syms)} symbols")
        prices.prefetch_daily(daily_syms + [cfg["benchmark"]])
    bench_d = prices.daily(cfg["benchmark"])
    bench_5 = prices.intraday(cfg["benchmark"]) if any(s["horizon"] == "intraday" for s in sigs) else None

    from .adapters import ist_now
    now = ist_now()
    counts: dict[str, int] = {}
    for s in sigs:
        try:
            if s["horizon"] == "daily":
                status, m = grade_daily(s, prices.daily(s["symbol"]), bench_d, cfg, asof=asof)
            else:
                status, m = grade_intraday(s, prices.intraday(s["symbol"]), bench_5, cfg, now=now)
        except Exception as e:  # never let one symbol stop the run
            status, m = "partial", {"error": f"{type(e).__name__}: {e}"[:200]}
        store.save_outcome(con, s["id"], status, m)
        counts[status] = counts.get(status, 0) + 1
    con.commit()
    log(f"validate: {len(sigs)} signals → " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return {"graded": len(sigs), **counts}
