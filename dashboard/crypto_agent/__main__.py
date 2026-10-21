"""
python -m crypto_agent live        fetch fresh candles, update paper trades, rebuild page (every 15 min)
python -m crypto_agent backtest    test every strategy × stop on ~12 months of 5m data, pick recommended combos
python -m crypto_agent levels      backtest "trade only at my level" (price/time levels from 6h swings)
python -m crypto_agent swing       multi-day swing test: 3000/5000-pt targets, up to 10-day holds
python -m crypto_agent report      rebuild the page only
"""

import argparse
import sys

from . import backtest, live


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="crypto_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["live", "backtest", "levels", "swing", "report"])
    ap.add_argument("--months", type=int)
    a = ap.parse_args()
    if a.cmd == "backtest":
        backtest.run(months=a.months)
        live.update()
    elif a.cmd == "levels":
        import os
        from . import config, data, levels
        cfg = config.load()
        months = a.months or cfg["backtest_months"]
        frames = {c: data.history(c, cfg, months=months) for c in cfg["coins"]}
        res, tr = levels.backtest(frames, cfg)
        res.to_csv(os.path.join(config.REPORT_DIR, "levels_backtest.csv"), index=False)
        print(res.head(20).to_string())
    elif a.cmd == "swing":
        import os
        from . import config, data, swing
        cfg = config.load()
        months = a.months or cfg["backtest_months"]
        frames = {c: data.history(c, cfg, months=months) for c in cfg["coins"]}
        res, tr = swing.backtest(frames, cfg)
        res.to_csv(os.path.join(config.REPORT_DIR, "swing_backtest.csv"), index=False)
        if not tr.empty:
            tr.to_csv(os.path.join(config.REPORT_DIR, "swing_trades.csv.gz"), index=False, compression="gzip")
        print(res.head(25).to_string())
    elif a.cmd == "live":
        live.update()
    live.render()


main()
