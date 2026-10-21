"""
swing.py — daily swing setups on Nifty 200 stocks (long only: delivery trades).

Setups (signal on the daily close, entry at the next day's open):

  ath_break      close above the all-time high, first time in 20+ sessions
  half_reclaim   close crosses back above 50 % of the all-time high (from below)
  half_pullback  close falls into 0–10 % above 50 % of ATH, coming from higher up
                 (the "half-ATH as support" idea)
  donchian_55    close above the 55-day high while above the 200-day average
  high52_volume  close above the 52-week high on 1.5 × average volume
  rsi2_dip       above the 200-day average and RSI(2) < 10 (short pullback in an uptrend)

Each runs with and without a market filter (Nifty 50 above its 200-day average),
with three exits: stop / target in ATR(14) multiples, max 20 sessions.
One position per stock per setup at a time. A gap through the stop fills at the
open. Random-entry control: each trade replayed on a random day of the same
stock and year (and same market-filter state) with the same exits.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import data, stats

SETUPS = ["ath_break", "half_reclaim", "half_pullback", "donchian_55", "high52_volume", "rsi2_dip"]
LABEL = {
    "ath_break": "All-time-high breakout", "half_reclaim": "Reclaim of 50 % of ATH",
    "half_pullback": "Pullback to 50 % of ATH (support)", "donchian_55": "55-day breakout in an uptrend",
    "high52_volume": "52-week high on volume", "rsi2_dip": "RSI(2) dip in an uptrend",
}


def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(d: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=d.index)
    f["ath_prev"] = d["High"].cummax().shift(1)
    f["sma200"] = d["Close"].rolling(200).mean()
    f["atr"] = data.atr(d)
    f["rsi2"] = _rsi(d["Close"], 2)
    f["hi55"] = d["High"].rolling(55).max().shift(1)
    f["hi252"] = d["High"].rolling(252).max().shift(1)
    f["vol50"] = d["Volume"].rolling(50).mean().shift(1)
    return f


def _cooldown(sig: np.ndarray, n: int = 20) -> np.ndarray:
    out = np.zeros_like(sig)
    last = -10 ** 9
    for i in np.flatnonzero(sig):
        if i - last >= n:
            out[i] = True
            last = i
    return out


def signals(d: pd.DataFrame, f: pd.DataFrame, name: str) -> np.ndarray:
    c, h, v = d["Close"].to_numpy(), d["High"].to_numpy(), d["Volume"].to_numpy()
    ath = f["ath_prev"].to_numpy()
    enough = np.arange(len(d)) >= 252                     # a year of history before trusting the ATH
    with np.errstate(invalid="ignore"):
        if name == "ath_break":
            newhigh = h > ath
            recent = pd.Series(newhigh).shift(1).rolling(20, min_periods=1).max().fillna(0).to_numpy() > 0
            return enough & (c > ath) & ~recent
        lvl = 0.5 * ath
        if name == "half_reclaim":
            return _cooldown(enough & (c > lvl) & (np.roll(c, 1) <= np.roll(lvl, 1)))
        if name == "half_pullback":
            zone = (c >= lvl) & (c <= 1.1 * lvl)
            return _cooldown(enough & zone & (np.roll(c, 1) > 1.1 * np.roll(lvl, 1)))
        sma = f["sma200"].to_numpy()
        if name == "donchian_55":
            return (c > f["hi55"].to_numpy()) & (c > sma)
        if name == "high52_volume":
            return (c > f["hi252"].to_numpy()) & (v > 1.5 * f["vol50"].to_numpy())
        if name == "rsi2_dip":
            return (c > sma) & (f["rsi2"].to_numpy() < 10)
    raise ValueError(name)


class Arr:
    """numpy views of one stock's bars + features (pandas access is too slow in loops)."""
    def __init__(self, d, f):
        self.idx = d.index
        self.o, self.h, self.l, self.c = (d[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        self.atr = f["atr"].to_numpy(float)
        self.n = len(d)


def simulate(A: Arr, t, stop_atr, tgt_atr, cost, max_hold):
    """Signal on day t → entry at the open of t+1. Returns a trade dict (or None)."""
    o, h, l, c = A.o, A.h, A.l, A.c
    if t + 1 >= A.n:
        return {"status": "pending", "signal_ts": A.idx[t], "entry_ts": None, "entry": np.nan,
                "stop": np.nan, "target_px": np.nan, "R": np.nan, "atr": float(A.atr[t])}
    a = float(A.atr[t])
    if not np.isfinite(a) or a <= 0:
        return None
    e = t + 1
    entry = o[e]
    stop, tgt = entry - stop_atr * a, entry + tgt_atr * a
    ex = None
    last = min(A.n - 1, t + max_hold)
    for j in range(e, last + 1):
        if l[j] <= stop:
            ex = (min(o[j], stop) if j > e else stop, "stop", j)
        elif h[j] >= tgt:
            ex = (max(o[j], tgt) if j > e else tgt, "target", j)
        elif j == t + max_hold:
            ex = (c[j], "time", j)
        if ex:
            break
    out = {"signal_ts": A.idx[t], "entry_ts": A.idx[e], "entry": entry, "stop": stop, "target_px": tgt,
           "stop_pct": stop_atr * a / entry * 100, "atr": a}
    if ex:
        out.update(exit_ts=A.idx[ex[2]], exit=ex[0], outcome=ex[1], status="closed", exit_i=ex[2])
    else:
        out.update(exit_ts=A.idx[last], exit=c[last], outcome="open", status="open", exit_i=A.n)
    out["net_pct"] = (out["exit"] - entry) / entry * 100 - 2 * cost * 100
    out["R"] = out["net_pct"] / out["stop_pct"]
    return out


def run(A: Arr, sig, stop_atr, tgt_atr, cost, max_hold, start_i=0):
    """One position at a time through a boolean signal array."""
    res, busy_until = [], -1
    for t in np.flatnonzero(sig):
        if t < start_i or t <= busy_until:
            continue
        tr = simulate(A, t, stop_atr, tgt_atr, cost, max_hold)
        if tr is None:
            continue
        res.append(tr)
        busy_until = tr.get("exit_i", A.n)
    return res


def market_ok(daily: dict, idx: pd.DatetimeIndex) -> np.ndarray:
    n = daily.get("^NSEI")
    if n is None or n.empty:
        return np.ones(len(idx), bool)
    ok = (n["Close"] > n["Close"].rolling(200).mean()).reindex(idx, method="ffill")
    return ok.fillna(False).to_numpy(bool)


def backtest(daily: dict, cfg: dict, years: int = 10, log=print):
    cost, hold = cfg["swing_cost_per_side"], cfg["swing_max_hold"]
    start = pd.Timestamp.now().normalize() - pd.DateOffset(years=years)
    rng = np.random.default_rng(11)
    real, fake = [], []
    syms = [s for s in daily if not s.startswith("^")]
    for n, sym in enumerate(syms):
        d = daily[sym]
        if len(d) < 300:
            continue
        f = features(d)
        A = Arr(d, f)
        mkt = market_ok(daily, d.index)
        start_i = int(np.searchsorted(d.index, start))
        valid = np.flatnonzero(np.isfinite(f["atr"].to_numpy()) & (np.arange(len(d)) >= max(start_i, 252)))
        valid = valid[valid < len(d) - 1]
        years_of = d.index.year.to_numpy()
        by_year = {}
        for y in np.unique(years_of[valid]):
            p = valid[years_of[valid] == y]
            by_year[(y, False)], by_year[(y, True)] = p, p[mkt[p]]
        for name in SETUPS:
            sig = signals(d, f, name)
            for filt in (False, True):
                s2 = sig & mkt if filt else sig
                for sa, ta in cfg["swing_exits"]:
                    trs = [t for t in run(A, s2, sa, ta, cost, hold, start_i) if t["status"] == "closed"]
                    key = {"sym": sym, "strategy": name, "market_filter": filt, "exit_rule": f"{sa:g}/{ta:g} ATR"}
                    for t in trs:
                        real.append({**t, **key, "side": "long"})
                        pool = by_year.get((t["entry_ts"].year, filt))
                        if pool is None or not len(pool):
                            continue
                        for t2 in pool[rng.integers(0, len(pool), 3)]:
                            x = simulate(A, int(t2), sa, ta, cost, hold)
                            if x and x["status"] == "closed":
                                fake.append({**key, "R": x["R"]})
        if n % 50 == 49:
            log(f"swing: {n + 1}/{len(syms)} stocks, {len(real)} trades")
    tr = pd.DataFrame(real)
    if tr.empty:
        return pd.DataFrame(), tr
    fk = pd.DataFrame(fake)
    rand = fk.groupby(["strategy", "market_filter", "exit_rule"])["R"].mean().to_dict() if not fk.empty else {}
    rows = []
    for (name, filt, ex), g in tr.groupby(["strategy", "market_filter", "exit_rule"]):
        s = stats.summarize(g, cfg, rand.get((name, filt, ex)), cluster="week")
        rows.append({"strategy": name, "market_filter": filt, "exit_rule": ex, **s,
                     "per_month": round(s["trades"] / (years * 12), 1),
                     "avg_hold_d": round(float((pd.to_datetime(g.exit_ts) - pd.to_datetime(g.entry_ts)).dt.days.mean()), 1),
                     "span": f"{g.entry_ts.min():%Y-%m-%d} → {g.entry_ts.max():%Y-%m-%d}"})
    res = pd.DataFrame(rows).sort_values("exp_R", ascending=False)
    return res, tr.drop(columns=["exit_i"], errors="ignore")
