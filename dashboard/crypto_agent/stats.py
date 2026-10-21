"""stats.py — trade-list statistics shared by the swing backtest and tracker."""

import numpy as np
import pandas as pd


def _stats(tr: pd.DataFrame) -> dict:
    c = tr[tr["status"] == "closed"]
    if c.empty:
        return {"trades": 0}
    r = c["R"]
    eq = r.cumsum()
    dd = float((eq.cummax() - eq).max())
    losses = (r <= 0).astype(int)
    streak = int(losses.groupby((losses != losses.shift()).cumsum()).cumsum().max())
    gp, gl = c.loc[c.net_pts > 0, "net_pts"].sum(), -c.loc[c.net_pts <= 0, "net_pts"].sum()
    return {
        "trades": len(c),
        "target_hit%": round((c["outcome"] == "win").mean() * 100, 1),
        "success%": round((c["net_pts"] > 0).mean() * 100, 1),
        "exp_R": round(float(r.mean()), 3),
        "t_stat": round(float(r.mean() / (r.std() / np.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else 0.0,
        "total_R": round(float(r.sum()), 1),
        "net_pts": round(float(c["net_pts"].sum()), 0),
        "PF": round(float(gp / gl), 2) if gl > 0 else None,
        "max_dd_R": round(dd, 1),
        "worst_streak": streak,
        "avg_cost_pts": round(float(c["cost_pts"].mean()), 1),
    }
