"""
strategies.py — intraday trade ideas for BTC / ETH, and a fee-aware simulator.

Every idea is evaluated per UTC day and produces ORDERS that are only known
from data available at that moment (no look-ahead):

  orb        opening-range breakout: range of the first N minutes after a
             session start; buy-stop above / sell-stop below (first fill wins)
  asia_break range 00:00–08:00 UTC, trade its break during London + US
  pdhl_break previous UTC day's high / low broken today
  pdhl_fade  sell the first touch of yesterday's high, buy yesterday's low
  sq9_break  Gann Square-of-9 rungs from the daily open (90° / 180°), broken
  sq9_fade   the same rungs, faded

Every order shares one exit rule set: fixed TARGET (1000 BTC points, ETH the
same %), fixed STOP (from the stop grid), and a time exit at the end of the
UTC day. Fees + slippage are charged on both sides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Order:
    side: str          # long / short
    kind: str          # stop / limit
    level: float
    active_from: pd.Timestamp
    expires: pd.Timestamp


# ── strategy catalogue: name -> (family, params) ─────────────────────────────

CATALOGUE: dict[str, tuple[str, dict]] = {
    "orb_utc_30m":    ("orb", {"start": "00:00", "minutes": 30, "until": "20:00"}),
    "orb_utc_60m":    ("orb", {"start": "00:00", "minutes": 60, "until": "20:00"}),
    "orb_london_60m": ("orb", {"start": "08:00", "minutes": 60, "until": "20:00"}),
    "orb_us_30m":     ("orb", {"start": "13:30", "minutes": 30, "until": "21:00"}),
    "asia_break":     ("orb", {"start": "00:00", "minutes": 480, "until": "20:00"}),
    "pdhl_break":     ("pdhl", {"mode": "break"}),
    "pdhl_fade":      ("pdhl", {"mode": "fade"}),
    "sq9_90_break":   ("sq9", {"deg": 90, "mode": "break"}),
    "sq9_180_break":  ("sq9", {"deg": 180, "mode": "break"}),
    "sq9_90_fade":    ("sq9", {"deg": 90, "mode": "fade"}),
    "sq9_180_fade":   ("sq9", {"deg": 180, "mode": "fade"}),
}


def _at(day: pd.Timestamp, hm: str) -> pd.Timestamp:
    h, m = map(int, hm.split(":"))
    return day + pd.Timedelta(hours=h, minutes=m)


def orders_for_day(name: str, day: pd.Timestamp, bars: pd.DataFrame, prev: pd.DataFrame | None) -> list[Order]:
    """bars = this UTC day's 5m bars (only the part needed is read)."""
    fam, p = CATALOGUE[name]
    end = day + pd.Timedelta(hours=23, minutes=55)
    if bars.empty:
        return []
    if fam == "orb":
        s = _at(day, p["start"])
        e = s + pd.Timedelta(minutes=p["minutes"])
        rng = bars[(bars.index >= s) & (bars.index < e)]
        if len(rng) < max(2, p["minutes"] // 5 - 1):
            return []
        hi, lo = float(rng["High"].max()), float(rng["Low"].min())
        until = min(_at(day, p["until"]), end)
        return [Order("long", "stop", hi, e, until), Order("short", "stop", lo, e, until)]
    if fam == "pdhl":
        if prev is None or prev.empty:
            return []
        pdh, pdl = float(prev["High"].max()), float(prev["Low"].min())
        o = float(bars["Open"].iloc[0])
        if not (pdl < o < pdh):
            return []                       # opened outside yesterday's range: no clean level
        start = day + pd.Timedelta(minutes=5)
        if p["mode"] == "break":
            return [Order("long", "stop", pdh, start, end), Order("short", "stop", pdl, start, end)]
        return [Order("short", "limit", pdh, start, end), Order("long", "limit", pdl, start, end)]
    if fam == "sq9":
        o = float(bars["Open"].iloc[0])
        step = p["deg"] / 180.0
        up = (math.sqrt(o) + step) ** 2
        dn = (math.sqrt(o) - step) ** 2
        start = day + pd.Timedelta(minutes=5)
        until = _at(day, "20:00")
        if p["mode"] == "break":
            return [Order("long", "stop", up, start, until), Order("short", "stop", dn, start, until)]
        return [Order("short", "limit", up, start, until), Order("long", "limit", dn, start, until)]
    raise ValueError(name)


# ── simulator ────────────────────────────────────────────────────────────────

def simulate(orders: list[Order], bars: pd.DataFrame, target_pts: float, stop_pts: float,
             cost_frac: float, day_end: pd.Timestamp, now: pd.Timestamp | None = None) -> dict:
    """
    First order to fill wins (one trade per strategy per day).
    Ambiguity rules (conservative):
      • both orders trigger in the same bar → the one nearer that bar's open fills
      • on the fill bar the target only counts if the bar CLOSES beyond it;
        the stop counts if touched
      • on later bars, stop is checked before target
      • gap through stop/target fills at the open
    Returns a dict with status pending | open | closed | expired.
    """
    if not orders:
        return {"status": "none"}
    first = min(o.active_from for o in orders)
    last = max(o.expires for o in orders)
    win = bars[(bars.index >= first) & (bars.index <= day_end)]
    O, H, L, C = (win[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    idx = win.index
    fill = side = fill_i = None
    for i in range(len(win)):
        t = idx[i]
        if fill is None:
            if t > last:
                return {"status": "expired"}
            cands = []
            for o in orders:
                if not (o.active_from <= t <= o.expires):
                    continue
                trig_up = o.side == "long" if o.kind == "stop" else o.side == "short"
                if trig_up and H[i] >= o.level:
                    cands.append((abs(o.level - O[i]), o, max(O[i], o.level)))
                elif not trig_up and L[i] <= o.level:
                    cands.append((abs(o.level - O[i]), o, min(O[i], o.level)))
            if not cands:
                continue
            _, o, px = min(cands, key=lambda x: x[0])
            fill, side, fill_i = px, o.side, i
            sgn = 1 if side == "long" else -1
            stop = fill - sgn * stop_pts
            tgt = fill + sgn * target_pts
            # fill bar: stop if touched, target only on a close beyond
            if (side == "long" and L[i] <= stop) or (side == "short" and H[i] >= stop):
                return _closed(side, idx[fill_i], fill, t, stop, stop_pts, cost_frac, "loss", stop, tgt)
            if (side == "long" and C[i] >= tgt) or (side == "short" and C[i] <= tgt):
                return _closed(side, idx[fill_i], fill, t, tgt, stop_pts, cost_frac, "win", stop, tgt)
            continue
        if side == "long":
            if O[i] <= stop:  return _closed(side, idx[fill_i], fill, t, O[i], stop_pts, cost_frac, "loss", stop, tgt)
            if O[i] >= tgt:   return _closed(side, idx[fill_i], fill, t, O[i], stop_pts, cost_frac, "win", stop, tgt)
            if L[i] <= stop:  return _closed(side, idx[fill_i], fill, t, stop, stop_pts, cost_frac, "loss", stop, tgt)
            if H[i] >= tgt:   return _closed(side, idx[fill_i], fill, t, tgt, stop_pts, cost_frac, "win", stop, tgt)
        else:
            if O[i] >= stop:  return _closed(side, idx[fill_i], fill, t, O[i], stop_pts, cost_frac, "loss", stop, tgt)
            if O[i] <= tgt:   return _closed(side, idx[fill_i], fill, t, O[i], stop_pts, cost_frac, "win", stop, tgt)
            if H[i] >= stop:  return _closed(side, idx[fill_i], fill, t, stop, stop_pts, cost_frac, "loss", stop, tgt)
            if L[i] <= tgt:   return _closed(side, idx[fill_i], fill, t, tgt, stop_pts, cost_frac, "win", stop, tgt)
    if fill is None:
        done = len(idx) and idx[-1] >= min(last, day_end - pd.Timedelta(minutes=5))
        return {"status": "expired" if done else "pending",
                "orders": [(o.side, o.kind, round(o.level, 2)) for o in orders]}
    if len(idx) and idx[-1] >= day_end - pd.Timedelta(minutes=5):
        return _closed(side, idx[fill_i], fill, idx[-1], C[-1], stop_pts, cost_frac, "time_exit", stop, tgt)
    sgn = 1 if side == "long" else -1
    return {"status": "open", "side": side, "entry_ts": idx[fill_i], "entry": fill, "stop": stop, "target": tgt,
            "last": C[-1], "unreal_pts": sgn * (C[-1] - fill)}


def _closed(side, ets, entry, xts, exit_, stop_pts, cost_frac, outcome, stop, tgt):
    sgn = 1 if side == "long" else -1
    gross = sgn * (exit_ - entry)
    cost = (entry + exit_) * cost_frac
    net = gross - cost
    return {"status": "closed", "side": side, "entry_ts": ets, "entry": entry, "exit_ts": xts,
            "exit": exit_, "stop": stop, "target": tgt, "outcome": outcome,
            "gross_pts": gross, "cost_pts": cost, "net_pts": net, "R": net / stop_pts}


# ── run one strategy over a span of days ─────────────────────────────────────

def run(name: str, df: pd.DataFrame, btc_open: pd.Series | None, coin: str, btc_target: float,
        btc_stop: float, cost_frac: float, days: list[pd.Timestamp] | None = None) -> list[dict]:
    """btc_open: BTC daily open per UTC day (used to scale ETH to the same %)."""
    if df.empty:
        return []
    by_day = {d: g for d, g in df.groupby(df.index.floor("D"))}
    keys = sorted(by_day)
    days = days or keys
    out = []
    for d in days:
        bars = by_day.get(d)
        if bars is None or len(bars) < 12:
            continue
        k = keys.index(d)
        prev = by_day.get(keys[k - 1]) if k > 0 else None
        if coin == "BTC":
            tgt, stp = btc_target, btc_stop
        else:
            ref = btc_open.get(d) if btc_open is not None else None
            if not ref or not np.isfinite(ref):
                continue
            pct = float(bars["Open"].iloc[0]) / ref
            tgt, stp = btc_target * pct, btc_stop * pct
        orders = orders_for_day(name, d, bars, prev)
        res = simulate(orders, bars, tgt, stp, cost_frac, d + pd.Timedelta(hours=23, minutes=55))
        if res["status"] == "none":
            continue
        res.update({"day": d, "strategy": name, "coin": coin, "btc_stop": btc_stop,
                    "target_pts": tgt, "stop_pts": stp})
        out.append(res)
    return out
