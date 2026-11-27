"""
config.py — settings for the Gann Square Blueprint EOD screener.

Override any key with gann_square_agent/config.json (top-level keys replace,
"blueprint" is merged key by key).
"""

from __future__ import annotations

import copy
import json
import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.dirname(AGENT_DIR)
STATE_DIR = os.path.join(AGENT_DIR, "state")       # results, page, alert log  → gannsquare/ on agent-data (public page)
STORE_DIR = os.path.join(AGENT_DIR, "store")       # NSE price history         → nse-eod-data branch (not published)
for _d in (STATE_DIR, STORE_DIR, os.path.join(STORE_DIR, "hist"), os.path.join(STORE_DIR, "actions")):
    os.makedirs(_d, exist_ok=True)

DEFAULTS: dict = {
    # Universe: "all" = every NSE equity in NSE's EQUITY_L list (series below),
    # or a name from screener.UNIVERSES ("nifty500", "nifty200", "fno", ...), or a list of symbols.
    "universe": "all",
    "series": ["EQ", "BE"],
    "history_years": 5,              # all NSE MCP keeps (≈ 20 calls per stock to bootstrap, once)
    "workers": 4,                    # parallel MCP sessions
    "budget_minutes_eod": 75,        # daily run: update + continue the backfill for this long
    "budget_minutes_bootstrap": 320, # first fill (GitHub's job limit is 360)
    "min_bars": 60,                  # skip stocks with less history than this
    "min_turnover_cr": 0.0,          # optional liquidity filter: 20-day average traded value, ₹ crore (0 = off)

    # Pine inputs — same names/defaults as the indicator; change here to match your chart settings.
    "blueprint": {
        "sw_mode": "ATR zigzag (significant swings)",   # or "Pivot (N bars each side)"
        "zz_mult": 3.0,
        "w": 10,
        "use_prov": True,
        "tol_pct": 10.0,
        "ang_scale": 0.0,
        "ang_bars": 150,
        "side_on": True,
        "n_clone": 1,
    },

    # Alerts — same switches as the Pine "Alerts" group
    "sq_alert": True,                # PRICE-TIME SQUARE NOW (once per square)
    "watch_alert": False,            # time square due, price not on a level yet (early warning)
    "alerts_per_message": 12,
}


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = os.path.join(AGENT_DIR, "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        bp = user.pop("blueprint", {})
        cfg.update(user)
        cfg["blueprint"].update(bp)
    return cfg
