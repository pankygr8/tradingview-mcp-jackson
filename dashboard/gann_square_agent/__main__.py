"""
python -m gann_square_agent fetch [--budget MIN]   update NSE prices from NSE's MCP server (+ continue the backfill)
python -m gann_square_agent publish                scan all stocks, build the page, send Telegram alerts
python -m gann_square_agent eod                    fetch + publish (what the daily run does)
python -m gann_square_agent bootstrap              fetch with the long budget, then publish (first run)
python -m gann_square_agent report                 rebuild the page only
python -m gann_square_agent probe                  check the NSE MCP server: tools + one sample call each
python -m gann_square_agent test-alert             send a test Telegram message
"""

import argparse
import json
import os
import sys

from . import config


def _probe(log=print):
    from .nse_mcp import NSEMCP, rows_of
    c = NSEMCP()
    info = c.connect()
    tools = c.list_tools()
    log(f"server: {info} · {len(tools)} tools: {', '.join(t.get('name', '?') for t in tools)}")
    out = {"server": info, "tools": [{"name": t.get("name"), "inputSchema": t.get("inputSchema")} for t in tools]}
    for name, args in (("get_stock_history", {"symbol": "RELIANCE", "months": 1, "endDate": "today"}),
                       ("get_bulk_quote", {"symbols": ["RELIANCE", "TCS", "M&M"]}),
                       ("get_corporate_actions", {"symbol": "RELIANCE", "fromDate": "2024-01-01", "toDate": "2026-12-31"})):
        try:
            p = c.call(name, args)
            rows = rows_of(p)
            out[name] = {"keys": list(p.keys()) if isinstance(p, dict) else type(p).__name__,
                         "rows": len(rows), "first_row": rows[0] if rows else None,
                         "last_row": rows[-1] if rows else None}
            log(f"{name}: {len(rows)} row(s); fields {list(rows[0].keys()) if rows else '—'}")
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            log(f"{name}: FAILED {type(e).__name__}: {e}")
    with open(os.path.join(config.STATE_DIR, "probe.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    return out


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if config.DASHBOARD_DIR not in sys.path:
        sys.path.insert(0, config.DASHBOARD_DIR)
    ap = argparse.ArgumentParser(prog="gann_square_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "publish", "eod", "bootstrap", "report", "probe", "test-alert"])
    ap.add_argument("--budget", type=float, help="minutes for fetching (default from config)")
    a = ap.parse_args()
    cfg = config.load()
    from . import data, notify, page, scan

    if a.cmd == "test-alert":
        ok = notify.send("✅ Test alert from your Gann Square Blueprint screener.\n" + notify.PAGE)
        print("test alert sent" if ok else "test alert NOT sent")
        return
    if a.cmd == "probe":
        _probe()
        return
    info = None
    if a.cmd in ("fetch", "eod", "bootstrap"):
        budget = a.budget or cfg["budget_minutes_bootstrap" if a.cmd == "bootstrap" else "budget_minutes_eod"]
        info = data.sync(cfg, budget)
        if a.cmd == "fetch":
            return
    if a.cmd in ("publish", "eod", "bootstrap"):
        res = scan.run(cfg)
        notify.check(res, cfg)
    print("page:", page.render(cfg, info))


main()
