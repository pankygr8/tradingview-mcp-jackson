"""stats.py — trade statistics and the verdict rule shared by both backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(t: pd.DataFrame, cfg: dict, placebo_R: float | None, cluster: str = "day") -> dict:
    """cluster: trades on the same day (intraday) or week (swing) move together, so the
    t-stat is computed on the per-cluster average R — otherwise 50 stocks breaking out on
    one strong day would count as 50 independent pieces of evidence."""
    if t.empty:
        return {"trades": 0}
    t = t.sort_values("entry_ts")
    r = t["R"].to_numpy(float)
    n = len(r)
    mid = t["entry_ts"].iloc[n // 2]
    h1, h2 = t[t.entry_ts < mid]["R"].mean(), t[t.entry_ts >= mid]["R"].mean()
    day = pd.to_datetime(t["entry_ts"].astype(str).str[:10])
    key = day if cluster == "day" else day.dt.to_period("W")
    cm = t.groupby(key.values)["R"].mean().to_numpy(float)
    sd = cm.std(ddof=1) if len(cm) > 2 else 0.0
    tstat = float(cm.mean() / (sd / np.sqrt(len(cm)))) if sd > 0 else 0.0
    yrs = t.groupby(day.dt.year.values)["R"].mean()
    gp, gl = r[r > 0].sum(), -r[r <= 0].sum()
    eq = np.cumsum(r)
    out = {
        "trades": n,
        "days": len(cm),
        "success%": round(float((r > 0).mean() * 100), 1),
        "target_hit%": round(float((t["outcome"] == "target").mean() * 100), 1),
        "exp_R": round(float(r.mean()), 3),
        "exp_R_h1": round(float(h1), 3) if pd.notna(h1) else None,
        "exp_R_h2": round(float(h2), 3) if pd.notna(h2) else None,
        "t_stat": round(tstat, 2),
        "avg_net%": round(float(t["net_pct"].mean()), 3),
        "PF": round(float(gp / gl), 2) if gl > 0 else None,
        "max_dd_R": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
        "years_positive": f"{int((yrs > 0).sum())}/{len(yrs)}",
        "random_R": None if placebo_R is None or pd.isna(placebo_R) else round(float(placebo_R), 3),
    }
    out["verdict"] = verdict(out, cfg)
    return out


def verdict(s: dict, cfg: dict) -> str:
    if s["trades"] < cfg["min_trades"]:
        return "too few trades"
    both = (s["exp_R_h1"] or 0) > 0 and (s["exp_R_h2"] or 0) > 0
    beats = s["random_R"] is None or s["exp_R"] - s["random_R"] >= cfg["min_edge_over_random"]
    if both and s["t_stat"] >= cfg["min_t_stat"] and beats:
        return "robust edge"
    if both and s["exp_R"] > 0:
        return "positive, not significant" if beats else "positive, but random entries do as well"
    return "no edge"
