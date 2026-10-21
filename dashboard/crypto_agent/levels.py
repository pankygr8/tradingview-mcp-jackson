"""
levels.py — "trade only at my level": price/time levels derived by rule, traded
only when price comes to them.

This codifies a reading of the hand-drawn "invisible trendline" method
(btc_invisible_trendlines.pine) so it can be backtested without hindsight.
Every level is built only from swing pivots that were ALREADY CONFIRMED at the
time, and is traded at most once (its first touch).

Swing pivots
  6-hour bars (UTC 00/06/12/18). A swing high/low is a bar whose high/low is
  the extreme of `wing` bars on each side; it is only known `wing` bars later
  (18 h for wing=3) — the level is born at that confirmation time.

Level families (each line = anchor time/price + slope, extended right)
  trendline   through the last two swing highs if the second is LOWER (falling
              resistance), or the last two swing lows if the second is HIGHER
              (rising support) — like lines C and D in the Pine script
  timing_1x1  Gann price×time square: from each new swing pivot, a line whose
              slope equals the previous swing leg's range ÷ its duration,
              pointing in the direction of the new leg — like lines A and B
  timing_1x2  same, at half that slope (Gann 1×2)
  projection  horizontal levels at 1.5× and 2× the last completed swing leg,
              projected from its end — like the 1.5x / 2x dotted lines
  pivot_hz    horizontal level at each confirmed swing high / low — like the
              support / resistance boxes

Trade rules (on 5-minute bars)
  fade          first touch: price coming from above → long (support), from
                below → short (resistance); limit fill at the level
  break_retest  first 5-min close beyond the level by `break_buf`, then the
                first return to the level within 24 h → trade in the break's
                direction at the level
  Exits: target 1000 BTC points (ETH same %), stop from the grid, time exit
  after `max_hold_h`. Same conservative fill rules and costs as strategies.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FAMILIES = ("trendline", "timing_1x1", "timing_1x2", "projection", "pivot_hz")
RULES = ("fade", "break_retest")


@dataclass
class Line:
    family: str
    born: pd.Timestamp       # first moment the line is known
    t0: pd.Timestamp         # anchor time
    p0: float                # anchor price
    slope: float             # price per second (0 for horizontals)
    note: str = ""

    def value(self, ts_sec: np.ndarray) -> np.ndarray:
        return self.p0 + self.slope * (ts_sec - self.t0.timestamp())


# ── swing pivots on 6h bars ──────────────────────────────────────────────────

def pivots(df5: pd.DataFrame, wing: int = 3, tf: str = "6h") -> list[dict]:
    kw = {"origin": "epoch"} if pd.Timedelta(tf) < pd.Timedelta("1D") else {}
    b = df5.resample(tf, **kw).agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
    H, L, idx = b["High"].to_numpy(), b["Low"].to_numpy(), b.index
    step = pd.Timedelta(tf)
    out = []
    for i in range(wing, len(b) - wing):
        win_h, win_l = H[i - wing:i + wing + 1], L[i - wing:i + wing + 1]
        conf = idx[i + wing] + step                  # end of the confirming bar
        if H[i] == win_h.max() and (win_h == H[i]).sum() == 1:
            out.append({"kind": "H", "t": idx[i] + step / 2, "p": float(H[i]), "conf": conf})
        if L[i] == win_l.min() and (win_l == L[i]).sum() == 1:
            out.append({"kind": "L", "t": idx[i] + step / 2, "p": float(L[i]), "conf": conf})
    out.sort(key=lambda x: (x["conf"], x["t"]))
    # keep an alternating H/L sequence (two highs in a row → keep the higher)
    seq: list[dict] = []
    for p in out:
        if seq and seq[-1]["kind"] == p["kind"]:
            better = p["p"] > seq[-1]["p"] if p["kind"] == "H" else p["p"] < seq[-1]["p"]
            if better:
                seq[-1] = p
            continue
        seq.append(p)
    return seq


# ── level construction ───────────────────────────────────────────────────────

def build_lines(piv: list[dict]) -> list[Line]:
    lines: list[Line] = []
    highs = [p for p in piv if p["kind"] == "H"]
    lows = [p for p in piv if p["kind"] == "L"]
    for k, p in enumerate(piv):
        born = p["conf"]
        # horizontal at the pivot
        lines.append(Line("pivot_hz", born, p["t"], p["p"], 0.0, f"{p['kind']} {p['p']:.0f}"))
        if k >= 1:
            a = piv[k - 1]                               # previous pivot = start of the leg that just ended
            leg = p["p"] - a["p"]                        # + up-leg, − down-leg
            dur = (p["t"] - a["t"]).total_seconds()
            if dur > 0:
                speed = abs(leg) / dur
                # new leg runs opposite to the one that just ended
                sgn = -1 if leg > 0 else 1
                lines.append(Line("timing_1x1", born, p["t"], p["p"], sgn * speed, f"1x1 from {p['kind']}"))
                lines.append(Line("timing_1x2", born, p["t"], p["p"], sgn * speed / 2, f"1x2 from {p['kind']}"))
                # 1.5x / 2x projections of the completed leg, from its end
                for m in (1.5, 2.0):
                    lvl = a["p"] + leg * m
                    lines.append(Line("projection", born, p["t"], lvl, 0.0, f"{m}x leg"))
    # trendlines through consecutive lower highs / higher lows
    for seqs, kind in ((highs, "H"), (lows, "L")):
        for a, b in zip(seqs, seqs[1:]):
            falling_res = kind == "H" and b["p"] < a["p"]
            rising_sup = kind == "L" and b["p"] > a["p"]
            if not (falling_res or rising_sup):
                continue
            dur = (b["t"] - a["t"]).total_seconds()
            if dur <= 0:
                continue
            lines.append(Line("trendline", b["conf"], a["t"], a["p"], (b["p"] - a["p"]) / dur,
                              f"{'falling' if falling_res else 'rising'} {a['p']:.0f}->{b['p']:.0f}"))
    return lines


# ── trade simulation on 5m bars ──────────────────────────────────────────────

def _exit(O, H, L, C, i0, side, fill, stop, tgt, max_bars):
    """Bars i0.. after the fill bar. Returns (outcome, exit_price, exit_index)."""
    end = min(len(O), i0 + max_bars)
    long = side == "long"
    for i in range(i0, end):
        if long:
            if O[i] <= stop: return "loss", O[i], i
            if O[i] >= tgt:  return "win", O[i], i
            if L[i] <= stop: return "loss", stop, i
            if H[i] >= tgt:  return "win", tgt, i
        else:
            if O[i] >= stop: return "loss", O[i], i
            if O[i] <= tgt:  return "win", O[i], i
            if H[i] >= stop: return "loss", stop, i
            if L[i] <= tgt:  return "win", tgt, i
    if end >= len(O):
        return "open", C[-1], len(O) - 1
    return "time_exit", C[end - 1], end - 1


def trade_lines(lines: list[Line], df5: pd.DataFrame, rule: str, tgt_frac: pd.Series,
                stop_frac: pd.Series, cost: float, expire_days: float = 30, max_hold_h: float = 48,
                break_buf: float = 0.0015, retest_h: float = 24) -> list[dict]:
    """tgt_frac / stop_frac: target and stop as a FRACTION of price, per 5m bar
    (1000 / BTC price for the target), so BTC and ETH use the same % move."""
    idx = df5.index
    # seconds since epoch, independent of pandas' datetime resolution (ns in 2.x, us in 3.x)
    tsec = (idx - pd.Timestamp(0, tz="UTC")).total_seconds().to_numpy(float)
    O, H, L, C = (df5[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    tf, sf = tgt_frac.to_numpy(float), stop_frac.to_numpy(float)
    max_bars = int(max_hold_h * 12)
    out = []
    for ln in lines:
        s = idx.searchsorted(ln.born)
        e = idx.searchsorted(ln.born + pd.Timedelta(days=expire_days))
        if s >= len(idx) - 1 or e <= s + 1:
            continue
        v = ln.value(tsec[s:e])
        if rule == "fade":
            touch = np.where((L[s:e] <= v) & (H[s:e] >= v))[0]
            if not len(touch):
                continue
            j = touch[0]
            i = s + j
            if j == 0:
                continue                                  # born touching — no "approach" to judge
            prev_above = C[i - 1] > v[j - 1]
            side = "long" if prev_above else "short"
            lvl = v[j]
            fill = lvl if (O[i] >= lvl if side == "long" else O[i] <= lvl) else O[i]
        else:
            beyond_up = C[s:e] > v * (1 + break_buf)
            beyond_dn = C[s:e] < v * (1 - break_buf)
            # a break only counts if price started on the other side at birth
            start_above = C[s] > v[0]
            brk = np.where(beyond_dn if start_above else beyond_up)[0]
            if not len(brk):
                continue
            b = brk[0]
            side = "short" if start_above else "long"
            r_end = min(e - s, b + 1 + int(retest_h * 12))
            seg = slice(b + 1, r_end)
            back = np.where((L[s:e][seg] <= v[seg]) & (H[s:e][seg] >= v[seg]))[0]
            if not len(back):
                continue
            j = b + 1 + back[0]
            i = s + j
            lvl = v[j]
            fill = lvl if (O[i] >= lvl if side == "long" else O[i] <= lvl) else O[i]
        sgn = 1 if side == "long" else -1
        stop_pts, tgt_pts = fill * sf[i], fill * tf[i]
        stop, tgt = fill - sgn * stop_pts, fill + sgn * tgt_pts
        # fill bar: stop if touched; target only on a close beyond it
        if (side == "long" and L[i] <= stop) or (side == "short" and H[i] >= stop):
            outcome, px, xi = "loss", stop, i
        elif (side == "long" and C[i] >= tgt) or (side == "short" and C[i] <= tgt):
            outcome, px, xi = "win", tgt, i
        else:
            outcome, px, xi = _exit(O, H, L, C, i + 1, side, fill, stop, tgt, max_bars)
        if outcome == "open":
            continue
        gross = sgn * (px - fill)
        cst = (fill + px) * cost
        out.append({"family": ln.family, "rule": rule, "note": ln.note, "line_born": ln.born,
                    "entry_ts": idx[i], "day": idx[i].floor("D"), "side": side, "entry": fill,
                    "exit_ts": idx[xi], "exit": px, "outcome": outcome, "status": "closed",
                    "stop_pts": stop_pts, "target_pts": tgt_pts, "gross_pts": gross,
                    "cost_pts": cst, "net_pts": gross - cst, "R": (gross - cst) / stop_pts,
                    "hold_h": (idx[xi] - idx[i]).total_seconds() / 3600})
    return out


# ── backtest driver ──────────────────────────────────────────────────────────

def backtest(frames: dict, cfg: dict, wing: int = 3, log=print) -> tuple[pd.DataFrame, pd.DataFrame]:
    from .backtest import _stats
    btc = frames["BTC"]["Close"]
    cost = cfg["fee_per_side"] + cfg["slippage_per_side"]
    rows, trades = [], []
    for coin, df in frames.items():
        if df.empty:
            continue
        ref = btc.reindex(df.index, method="ffill").bfill()
        piv = pivots(df, wing=wing)
        lines = build_lines(piv)
        days = sorted(set(df.index.floor("D")))
        mid = days[len(days) // 2]
        log(f"levels: {coin} {len(piv)} pivots, {len(lines)} levels, {days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}")
        for rule in RULES:
            for stop in cfg["btc_stop_grid"]:
                tr = pd.DataFrame(trade_lines(lines, df, rule, cfg["btc_target_pts"] / ref, stop / ref, cost))
                if tr.empty:
                    continue
                tr["coin"], tr["btc_stop"] = coin, stop
                trades.append(tr)
                for fam in ("ALL",) + FAMILIES:
                    t = tr if fam == "ALL" else tr[tr.family == fam]
                    if t.empty:
                        continue
                    s, h1, h2 = _stats(t), _stats(t[t.day < mid]), _stats(t[t.day >= mid])
                    rows.append({"coin": coin, "family": fam, "rule": rule, "btc_stop": stop,
                                 "trades_per_month": round(s.get("trades", 0) / (len(days) / 30.4), 1),
                                 **s, "exp_R_h1": h1.get("exp_R"), "exp_R_h2": h2.get("exp_R"),
                                 "avg_hold_h": round(float(t.hold_h.mean()), 1),
                                 "span": f"{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}"})
    res = pd.DataFrame(rows)
    if res.empty:
        return res, pd.DataFrame()
    ok = ((res.trades >= cfg["min_trades"]) & (res.exp_R_h1 > 0) & (res.exp_R_h2 > 0)
          & (res.t_stat >= cfg.get("min_t_stat", 2.0)))
    res["verdict"] = np.where(ok, "robust edge",
                     np.where(res.trades < cfg["min_trades"], "too few trades",
                     np.where((res.exp_R_h1 > 0) & (res.exp_R_h2 > 0), "positive, not significant",
                     np.where(res.exp_R > 0, "unstable (one half only)", "no edge"))))
    res = res.sort_values("exp_R", ascending=False).reset_index(drop=True)
    return res, pd.concat(trades) if trades else pd.DataFrame()
