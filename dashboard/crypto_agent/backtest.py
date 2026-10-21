"""
backtest.py — test every strategy × stop size on BTC and ETH and pick the best.

Ranking is built to resist luck (the stock replay showed how much a result
can move when the sample shifts by a day):
  • the test period is split in two halves; a combo must be profitable
    (expectancy in R > 0, after fees) in BOTH halves to be recommended
  • combos are ranked by the WORSE of the two halves, not the average
  • at least `min_trades` trades are required

Outputs state/reports/crypto_backtest.csv and state/selection.json (the
recommended combos the live page highlights).
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import config, data, strategies


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


def run(months: int | None = None, cfg: dict | None = None, frames: dict | None = None, log=print) -> pd.DataFrame:
    cfg = cfg or config.load()
    months = months or cfg["backtest_months"]
    frames = frames or {c: data.history(c, cfg, months=months, log=log) for c in cfg["coins"]}
    btc = frames.get("BTC")
    btc_open = btc.groupby(btc.index.floor("D"))["Open"].first() if btc is not None and not btc.empty else None
    cost = cfg["fee_per_side"] + cfg["slippage_per_side"]

    rows, all_trades = [], []
    for coin, df in frames.items():
        if df.empty:
            log(f"backtest: no data for {coin}")
            continue
        days = sorted(set(df.index.floor("D")))
        mid = days[len(days) // 2]
        span = f"{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}"
        for name in strategies.CATALOGUE:
            for stop in cfg["btc_stop_grid"]:
                tr = pd.DataFrame(strategies.run(name, df, btc_open, coin, cfg["btc_target_pts"], stop, cost))
                if tr.empty:
                    continue
                all_trades.append(tr.assign(span=span))
                s = _stats(tr)
                h1 = _stats(tr[tr["day"] < mid])
                h2 = _stats(tr[tr["day"] >= mid])
                rows.append({"coin": coin, "strategy": name, "btc_stop": stop,
                             "rr": round(cfg["btc_target_pts"] / stop, 2), "days": len(days),
                             "trades_per_day": round(s.get("trades", 0) / max(len(days), 1), 2),
                             **s, "exp_R_h1": h1.get("exp_R"), "exp_R_h2": h2.get("exp_R"),
                             "trades_h1": h1.get("trades", 0), "trades_h2": h2.get("trades", 0), "span": span})
    res = pd.DataFrame(rows)
    if res.empty:
        log("backtest: nothing to rank")
        return res
    res["robust_R"] = res[["exp_R_h1", "exp_R_h2"]].min(axis=1)
    # profitable in both halves AND statistically distinguishable from zero —
    # with ~66 combos tested, "positive in both halves" alone passes a few by luck
    ok = ((res["trades"] >= cfg["min_trades"]) & (res["exp_R_h1"] > 0) & (res["exp_R_h2"] > 0)
          & (res["t_stat"] >= cfg.get("min_t_stat", 2.0)))
    res["verdict"] = np.where(ok, "robust edge",
                     np.where(res["trades"] < cfg["min_trades"], "too few trades",
                     np.where((res["exp_R_h1"] > 0) & (res["exp_R_h2"] > 0), "positive, not significant",
                     np.where(res["exp_R"] > 0, "unstable (one half only)", "no edge"))))
    res = res.sort_values(["verdict", "robust_R"], key=lambda s: s.map({"robust edge": 0, "positive, not significant": 1,
                                                                        "unstable (one half only)": 2,
                                                                        "no edge": 3, "too few trades": 4})
                          if s.name == "verdict" else -s).reset_index(drop=True)
    res.to_csv(os.path.join(config.REPORT_DIR, "crypto_backtest.csv"), index=False)
    if all_trades:
        pd.concat(all_trades).to_csv(os.path.join(config.REPORT_DIR, "crypto_backtest_trades.csv.gz"),
                                     index=False, compression="gzip")

    # one recommended stop per coin+strategy, top N per coin
    sel = []
    rob = res[res["verdict"] == "robust edge"]
    for coin, g in rob.groupby("coin"):
        g = g.sort_values("robust_R", ascending=False).drop_duplicates("strategy")
        sel += g.head(cfg["recommend_top"])[["coin", "strategy", "btc_stop", "exp_R", "robust_R", "trades",
                                             "success%"]].to_dict("records")
    with open(config.SELECTION_PATH, "w", encoding="utf-8") as f:
        json.dump({"generated": pd.Timestamp.now(tz="UTC").isoformat(), "recommended": sel}, f, indent=2, default=float)
    log(f"backtest: {len(res)} combos, {int(ok.sum())} robust, {len(sel)} recommended")
    return res
