"""
python -m crypto_agent live                 fetch fresh candles, update swing paper trades, rebuild page (every 15 min)
python -m crypto_agent swing [--months 36]  multi-day swing backtest: 3000/5000-pt targets, up to 10-day holds
python -m crypto_agent report               rebuild the page only
"""

import argparse
import glob
import os
import sys

from . import config, live

# files left on the data branch by the removed intraday code (21 Oct 2026)
OBSOLETE = ["crypto.db", "selection.json", "reports/crypto_backtest.csv", "reports/crypto_backtest_trades.csv.gz",
            "reports/levels_backtest.csv"]


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="crypto_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["live", "swing", "report"])
    ap.add_argument("--months", type=int)
    a = ap.parse_args()
    cfg = config.load()
    for rel in OBSOLETE:
        for p in glob.glob(os.path.join(config.STATE_DIR, rel)):
            os.remove(p)
    if a.cmd in ("live", "swing"):
        from . import data, swing_live
        months = (a.months or cfg["backtest_months"]) if a.cmd == "swing" else None
        frames = {c: data.history(c, cfg, months=months, live_only=a.cmd == "live") for c in cfg["coins"]}
        if a.cmd == "swing":
            from . import swing
            res, tr = swing.backtest(frames, cfg)
            res.to_csv(os.path.join(config.REPORT_DIR, "swing_backtest.csv"), index=False)
            if not tr.empty:
                tr.to_csv(os.path.join(config.REPORT_DIR, "swing_trades.csv.gz"), index=False, compression="gzip")
            print(res.head(25).to_string())
        swing_live.update(frames, cfg)
    live.render(cfg)


main()
