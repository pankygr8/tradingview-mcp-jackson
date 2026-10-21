"""
python -m nifty_agent backtest     download Nifty 200 prices, backtest intraday + swing setups, pick setups to track
python -m nifty_agent live         update paper trades (15-min bars in market hours, daily bars after 16:00 IST), page, alerts
python -m nifty_agent report       rebuild the page only
python -m nifty_agent test-alert   send a test Telegram message
"""

import argparse
import os
import sys

from . import config


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if config.DASHBOARD_DIR not in sys.path:
        sys.path.insert(0, config.DASHBOARD_DIR)
    ap = argparse.ArgumentParser(prog="nifty_agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["backtest", "live", "report", "test-alert"])
    ap.add_argument("--force", action="store_true", help="live: refresh data even outside market hours")
    a = ap.parse_args()
    cfg = config.load()
    from . import live, page
    if a.cmd == "test-alert":
        from . import notify
        ok = notify.send("✅ Test alert from your Nifty 200 screener.\n" + notify.PAGE)
        print("test alert sent" if ok else "test alert NOT sent")
        return
    if a.cmd == "backtest":
        import pandas as pd
        from . import data, intraday, swing
        syms = config.universe(cfg)
        daily = data.daily(syms + [cfg["benchmark"]], refresh=True)
        print(f"daily: {len(daily)} symbols")
        sres, strd = swing.backtest(daily, cfg)
        sres.to_csv(os.path.join(config.REPORT_DIR, "swing_backtest.csv"), index=False)
        strd.to_csv(os.path.join(config.REPORT_DIR, "swing_backtest_trades.csv.gz"), index=False, compression="gzip")
        print(sres.drop(columns=["span"]).head(20).to_string())
        parts, trs = [], []
        for interval, period, cache in (("1h", "730d", "h1"), ("15m", "60d", "m15")):
            frames = data.intraday(syms, interval, period, cache)
            print(f"{interval}: {len(frames)} symbols")
            r, t = intraday.backtest(frames, daily, interval, cfg)
            parts.append(r)
            trs.append(t)
        ires = pd.concat(parts, ignore_index=True)
        ires.to_csv(os.path.join(config.REPORT_DIR, "intraday_backtest.csv"), index=False)
        pd.concat(trs, ignore_index=True).to_csv(os.path.join(config.REPORT_DIR, "intraday_backtest_trades.csv.gz"),
                                                 index=False, compression="gzip")
        print(ires.drop(columns=["span"]).head(30).to_string())
        sel = live.select(ires, sres, cfg)
        print("selected:", sel)
        live.update(cfg, force=True)
    elif a.cmd == "live":
        live.update(cfg, force=a.force)
    page.render(cfg)


main()
