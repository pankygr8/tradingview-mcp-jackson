# Gann Square Blueprint — NSE EOD screener

The Pine indicator **Gann Square Blueprint** run on every NSE equity after the close.
Prices: NSE's official MCP server (`https://mcp.nseindia.in/bhavcopy/cm/mcp`, end-of-day bhavcopy, no login).
Page: `https://<user>.github.io/<repo>/gannsquare/` · alerts: Telegram (same bot/secrets as the other agents).

| File | What it does |
|---|---|
| `blueprint.py` | Line-by-line port of the Pine logic (zigzag/pivots, DNA swing, provisional DNA, the four squarings, swing angles, sideways filter, history test, table + alert texts). |
| `nse_mcp.py` | Small streamable-HTTP MCP client for NSE's server. |
| `data.py` | Universe (NSE `EQUITY_L`, series EQ + BE), 5-year backfill in 3-month chunks (resumable), daily update with `get_bulk_quote` (50 stocks per call), split/bonus adjustment from `get_corporate_actions`. |
| `scan.py` | Runs the blueprint on every stock with full history; `state/results.json`, `state/squares.csv`. |
| `page.py`, `notify.py` | The dashboard page and Telegram alerts (one alert per square, Pine wording). |

## Commands (from `dashboard/`)
```
python -m gann_square_agent probe        # check NSE's MCP server: tools + sample calls
python -m gann_square_agent bootstrap    # first download (long), then scan
python -m gann_square_agent eod          # daily: update prices, scan, page, alerts
python -m gann_square_agent report       # rebuild the page
python -m gann_square_agent test-alert
```

## Settings
`config.json` next to this file overrides `config.py`, e.g.
```json
{"universe": "nifty500", "min_turnover_cr": 1, "watch_alert": true,
 "blueprint": {"zz_mult": 2.5, "tol_pct": 10}}
```
`blueprint` keys are the Pine inputs (same defaults). Manual two-point DNA mode is not available in a screener.

## Notes
- Signals (DNA swing, levels, time squares, square / due / sideways) come out the same with 2, 3, 5 or 25 years of history.
  The **history test** ("rules that work") needs many swings: NSE's MCP keeps 5 years (~60 turns), while TradingView
  may load 20+ years (up to 400 turns), so that one row can read lower here than on your chart.
- Bhavcopy prices are unadjusted; the screener adjusts splits/bonus/rights like TradingView's default chart (no dividend adjustment).
