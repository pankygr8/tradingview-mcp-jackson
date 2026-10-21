"""
python -m crypto_agent live        fetch fresh candles, update paper trades, rebuild page (every 15 min)
python -m crypto_agent backtest    test every strategy × stop on ~12 months of 5m data, pick recommended combos
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
    ap.add_argument("cmd", choices=["live", "backtest", "report"])
    ap.add_argument("--months", type=int)
    a = ap.parse_args()
    if a.cmd == "backtest":
        backtest.run(months=a.months)
        live.update()
    elif a.cmd == "live":
        live.update()
    live.render()


main()
