"""
intraday.py — intraday setups on Nifty 200 stocks (15-minute or 1-hour bars).

Setups (one trade per stock per setup per day; signal on a CLOSED bar, entry at
the NEXT bar's open; square-off at 15:15; no new entries after 14:30):

  orb        opening-range breakout: first bar after the opening range that
             closes above the range high (long) / below the range low (short).
             Stop = other side of the range.
  orb_rvol   the same, only for "stocks in play": opening-bar volume at least
             2 × its average of the previous 10 sessions.
  pdhl       stock opens inside yesterday's range, then a bar closes above
             yesterday's high (long) / below yesterday's low (short).
             Stop = 0.3 × daily ATR(14).
  gap_go     gap of 1 %+ and the first bar closes strongly in the gap's
             direction (upper / lower half) → follow it. Stop = first bar's low / high.
  gap_fade   gap of 1 %+ but the first bar closes against the gap → trade
             back towards yesterday's close. Stop = first bar's high / low.

Targets: 1R, 2R, or hold to 15:15 (eod); gap_fade: yesterday's close (fill) or eod.
Stops are checked before targets when a bar touches both. Costs per side from config.

Random-entry control: every real trade is replayed at a random bar of a random
session of the same stock, same side, same stop distance and target — so
"random_R" is what the exits alone earn. A setup needs to beat it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, data, stats

IST = "Asia/Kolkata"
SETUPS = ["orb", "orb_rvol", "pdhl", "gap_go", "gap_fade"]
TARGETS = {"gap_fade": ["fill", "eod"]}
DEFAULT_TARGETS = ["1R", "2R", "eod"]
LABEL = {
    "orb": "Opening-range breakout", "orb_rvol": "Opening-range breakout, stocks in play (2× volume)",
    "pdhl": "Break of yesterday's high / low", "gap_go": "Gap and go", "gap_fade": "Gap fade",
}


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Stock:
    """One symbol's intraday bars, split into sessions, with daily context."""

    def __init__(self, sym: str, bars: pd.DataFrame, daily: pd.DataFrame | None, tf: int, cfg: dict):
        self.sym, self.tf = sym, tf
        self.idx = bars.index
        self.o, self.h, self.l, self.c, self.v = (bars[k].to_numpy(float) for k in data.COLS)
        mins = (self.idx.hour * 60 + self.idx.minute).to_numpy()
        self.end_min = mins + tf
        dates = self.idx.tz_convert(IST).date if self.idx.tz is not None else self.idx.date
        self.dates = np.array(dates)
        _, first = np.unique(self.dates, return_index=True)
        self.bounds = list(zip(first, list(first[1:]) + [len(self.dates)]))
        self.last_entry, self.exit_at = _hm(cfg["intraday_last_entry"]), _hm(cfg["intraday_exit"])
        # daily context: yesterday's close / high / low and ATR, keyed by session date
        self.ctx = {}
        if daily is not None and not daily.empty:
            dd = daily.copy()
            dd["atr"] = data.atr(dd)
            di = dd.index.date
            for d0 in sorted(set(self.dates)):
                k = np.searchsorted(di, d0) - 1           # last daily bar strictly before d0
                if k >= 0:
                    r = dd.iloc[k]
                    self.ctx[d0] = (float(r.Close), float(r.High), float(r.Low), float(r.atr))
        # opening-bar volume vs its 10-session average (stocks in play)
        ob = np.array([self.v[a] for a, _ in self.bounds], float)
        avg = pd.Series(ob).rolling(10, min_periods=10).mean().shift(1).to_numpy()
        self.rvol = {self.dates[a]: (ob[i] / avg[i] if avg[i] and avg[i] > 0 else np.nan)
                     for i, (a, _) in enumerate(self.bounds)}

    def session(self, k):
        a, b = self.bounds[k]
        d0 = self.dates[a]
        em = self.end_min[a:b]
        sig_last = int(np.searchsorted(em, self.last_entry, side="right")) - 1   # last bar allowed to signal
        exit_i = int(np.searchsorted(em, self.exit_at, side="right")) - 1         # bar whose close is the square-off
        return a, b, d0, sig_last, exit_i


# ── setup rules: return (signal bar, side, stop, fixed target or None) ───────
def _setups(st: Stock, a, b, d0, sig_last, name):
    o, h, l, c = st.o[a:b], st.h[a:b], st.l[a:b], st.c[a:b]
    if len(o) < 3 or sig_last < 0:
        return None
    ctx = st.ctx.get(d0)
    if name in ("orb", "orb_rvol"):
        if name == "orb_rvol" and not (st.rvol.get(d0, np.nan) >= 2.0):
            return None
        k = max(1, 15 // st.tf)                         # opening range = first 15 min (or first bar)
        hi, lo = h[:k].max(), l[:k].min()
        for i in range(k, sig_last + 1):
            if c[i] > hi:
                return (i, 1, lo, None)
            if c[i] < lo:
                return (i, -1, hi, None)
        return None
    if ctx is None:
        return None
    pc, pdh, pdl, atr = ctx
    if name == "pdhl":
        if not (pdl <= o[0] <= pdh) or not np.isfinite(atr):
            return None
        for i in range(0, sig_last + 1):
            if c[i] > pdh:
                return (i, 1, c[i] - 0.3 * atr, None)
            if c[i] < pdl:
                return (i, -1, c[i] + 0.3 * atr, None)
        return None
    gap = o[0] / pc - 1
    rng = h[0] - l[0]
    if abs(gap) < 0.01 or rng <= 0:
        return None
    pos = (c[0] - l[0]) / rng                           # where the first bar closed in its range
    if name == "gap_go":
        if gap > 0 and c[0] > o[0] and pos >= 0.5:
            return (0, 1, l[0], None)
        if gap < 0 and c[0] < o[0] and pos <= 0.5:
            return (0, -1, h[0], None)
        return None
    if name == "gap_fade":
        if gap > 0 and c[0] < o[0]:
            return (0, -1, h[:1].max(), pc)
        if gap < 0 and c[0] > o[0]:
            return (0, 1, l[:1].min(), pc)
    return None


def _target(entry, side, risk, spec, fixed):
    if spec == "eod":
        return None
    if spec == "fill":
        return fixed if fixed is not None and side * (fixed - entry) > 0 else np.nan
    return entry + side * float(spec[:-1]) * risk


def simulate(st: Stock, a, b, exit_i, e, side, stop, tgt, cost, n_avail=None):
    """Walk bars from entry index e (session-relative). Returns a trade dict.
    n_avail: number of session bars available (live); None = full session."""
    o, h, l, c = st.o[a:b], st.h[a:b], st.l[a:b], st.c[a:b]
    n_avail = len(o) if n_avail is None else n_avail
    entry = o[e]
    risk = side * (entry - stop)
    out = {"entry_ts": st.idx[a + e], "entry": entry, "stop": stop, "target_px": tgt,
           "stop_pct": risk / entry * 100}
    ex = None
    for j in range(e, min(exit_i, n_avail - 1) + 1):
        if side > 0 and l[j] <= stop:
            ex = (min(o[j], stop) if j > e else stop, "stop")
        elif side < 0 and h[j] >= stop:
            ex = (max(o[j], stop) if j > e else stop, "stop")
        elif tgt is not None and side * ((h[j] if side > 0 else l[j]) - tgt) >= 0:
            ex = (tgt, "target")
        elif j == exit_i:
            ex = (c[j], "eod")
        if ex:
            # exit time = the END of the bar in which it happened
            out.update(exit_ts=st.idx[a + j] + pd.Timedelta(minutes=st.tf), exit=ex[0], outcome=ex[1], status="closed")
            break
    if ex is None:
        last = min(exit_i, n_avail - 1)
        out.update(exit_ts=st.idx[a + last] + pd.Timedelta(minutes=st.tf), exit=c[last], outcome="open", status="open")
    out["net_pct"] = side * (out["exit"] - entry) / entry * 100 - 2 * cost * 100
    out["R"] = out["net_pct"] / out["stop_pct"]
    return out


def trades_for(st: Stock, k: int, name: str, cfg: dict, n_avail=None, rng=None):
    """All target variants of one setup on one session (plus random-entry twins if rng)."""
    a, b, d0, sig_last, exit_i = st.session(k)
    if n_avail is not None:
        sig_last = min(sig_last, n_avail - 1)
    s = _setups(st, a, b, d0, sig_last, name)
    if s is None:
        return [], []
    i, side, stop, fixed = s
    e = i + 1
    base = {"sym": st.sym, "strategy": name, "side": "long" if side > 0 else "short", "date": str(d0),
            "signal_ts": st.idx[a + i] + pd.Timedelta(minutes=st.tf)}
    if e > exit_i:
        return [], []
    if n_avail is not None and e >= n_avail:
        return [dict(base, target=t, status="pending", entry_ts=st.idx[a + i] + pd.Timedelta(minutes=st.tf),
                     entry=np.nan, stop=stop, target_px=np.nan, R=np.nan) for t in TARGETS.get(name, DEFAULT_TARGETS)], []
    entry = st.o[a + e]
    risk = side * (entry - stop)
    if risk <= 0:
        return [], []
    sp = risk / entry * 100
    if not (cfg["intraday_min_stop_pct"] <= sp <= cfg["intraday_max_stop_pct"]):
        return [], []
    cost = cfg["intraday_cost_per_side"]
    real, fake = [], []
    for spec in TARGETS.get(name, DEFAULT_TARGETS):
        tgt = _target(entry, side, risk, spec, fixed)
        if tgt is not None and np.isnan(tgt):
            continue
        t = simulate(st, a, b, exit_i, e, side, stop, tgt, cost, n_avail)
        real.append(dict(base, target=spec, **t))
        if rng is not None:
            # random session of the same stock (NOT the same day: a day that later
            # breaks out would hand a random earlier entry the breakout for free)
            for _ in range(3):
                a2, b2, _, sl2, ex2 = st.session(int(rng.integers(0, len(st.bounds))))
                if sl2 < 0 or ex2 < 1:
                    continue
                e2 = int(rng.integers(1, min(sl2 + 1, ex2) + 1))
                en2 = st.o[a2 + e2]
                st2 = en2 * (1 - side * sp / 100)
                tg2 = None if tgt is None else en2 * (1 + side * abs(tgt / entry - 1))
                f = simulate(st, a2, b2, ex2, e2, side, st2, tg2, cost)
                fake.append({"strategy": name, "target": spec, "side": base["side"], "R": f["R"]})
    return real, fake


def backtest(intra: dict, daily: dict, interval: str, cfg: dict, log=print):
    tf = data.bar_minutes(interval)
    rng = np.random.default_rng(7)
    real, fake = [], []
    for n, (sym, bars) in enumerate(intra.items()):
        if bars is None or len(bars) < 50:
            continue
        st = Stock(sym, bars, daily.get(sym), tf, cfg)
        for k in range(len(st.bounds)):
            for name in SETUPS:
                r, f = trades_for(st, k, name, cfg, rng=rng)
                real += r
                fake += f
        if n % 50 == 49:
            log(f"intraday {interval}: {n + 1}/{len(intra)} stocks, {len(real)} trades")
    tr = pd.DataFrame(real)
    if tr.empty:
        return pd.DataFrame(), tr
    tr["tf"] = interval
    fk = pd.DataFrame(fake)
    rand = fk.groupby(["strategy", "target", "side"])["R"].mean().to_dict() if not fk.empty else {}
    if not fk.empty:
        rand.update({(k[0], k[1], "both"): v for k, v in fk.groupby(["strategy", "target"])["R"].mean().items()})
    rows = []
    for (name, spec), g in tr.groupby(["strategy", "target"]):
        for side in ("long", "short", "both"):
            t = g if side == "both" else g[g.side == side]
            s = stats.summarize(t, cfg, rand.get((name, spec, side)), cluster="day")
            if s["trades"]:
                rows.append({"tf": interval, "strategy": name, "target": spec, "side": side, **s,
                             "per_day": round(s["trades"] / max(1, t["date"].nunique()), 1),
                             "span": f"{t.date.min()} → {t.date.max()}"})
    res = pd.DataFrame(rows).sort_values(["exp_R"], ascending=False)
    return res, tr
