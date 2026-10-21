"""
python -m commodity_agent backtest   10-year daily + 2-year hourly swing backtest, then start/refresh live tracking
python -m commodity_agent live       refresh data, update paper trades + alerts, rebuild page
python -m commodity_agent report     rebuild the page only
"""

import argparse
import sys

from . import config, engine, page


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="commodity_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["backtest", "live", "report"])
    a = ap.parse_args()
    cfg = config.load()
    if a.cmd == "backtest":
        engine.backtest(cfg)
        engine.live(cfg)
    elif a.cmd == "live":
        engine.live(cfg)
    page.render(cfg)


main()
