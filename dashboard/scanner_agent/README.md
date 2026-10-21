# Scanner agent

Runs every dashboard scanner on a schedule, records each pick, checks what the
price actually did afterwards, and ranks the scanners so you know which ones to
build a trade plan on.

```
scan (on the IST clock) ──► agent.db: signals ──► validate (real prices) ──► leaderboard + today's plan
       ▲                                                      ▲
replay (one-time, past year, daily scanners only) ────────────┘
```

## Scanners covered

| Key | Module | Buckets recorded | Horizon | Own stop/target |
|-----|--------|------------------|---------|-----------------|
| `ath` | screener.py | `ath_90d`, `half_ath_above`, `half_ath_below` | daily | no |
| `swing` | swing_screener.py | `tradeable`, `pullback`, `breakouts`, `coiled` | daily | yes (+ Gann time stop) |
| `fib_1d` | signal_engine.py (1d) | `buy`, `sell` (signal ≤ 1 bar old, still valid) | daily | no |
| `gann_fan_1d` | gann_fan_engine.py (1d) | `buy`, `sell`, `buy_confirmed`, `sell_confirmed` | daily | no |
| `intraday` | intraday_screener.py | `actionable`, `arming` | intraday | yes |
| `momentum` | Momentum_scanner.py | `actionable`, `arming` | intraday | yes |
| `fib_15m` | signal_engine.py (15m) | `buy`, `sell` — off by default | intraday | no |

The scanner files are imported unchanged.

## Quick start (Windows, from the `dashboard` folder)

```bat
run_agent.bat replay                 :: one-time: a year of history for the daily scanners (~2–10 min)
run_agent.bat report                 :: open scanner_agent\reports\leaderboard.html
install_agent_schedule.bat           :: then let Task Scheduler run it every 15 min
```

or keep a window open with `run_agent.bat daemon` instead of the scheduled task.

| Command | What it does |
|---------|--------------|
| `scan [--group daily\|intraday] [--only ath,swing]` | run scanners now and store new picks |
| `validate` | grade every open signal against real prices |
| `report` | rebuild `reports/leaderboard.html`, CSVs and `summary.md` |
| `replay [--days 365 --step 5 --universe nifty100 --only ath]` | historical back-fill (replaces earlier replay data unless `--keep`) |
| `cycle` | do whatever is due now (what the scheduler calls) |
| `daemon` | `cycle` every minute, forever |
| `status`, `picks` | what is stored / latest ranked picks in the console |

## Default schedule (IST, weekdays, skipped on NSE holidays)

| Time | Job |
|------|-----|
| 09:35 → 14:45, every 15 min | intraday scanners (`fno`) |
| 15:50 | daily scanners (ATH & swing on `nifty500`, fib & gann on `nifty200`) |
| 16:30 and 08:45 | validate |
| 16:45 | report |

Change anything by creating `scanner_agent/config.json` with just the keys you
want to override, e.g.

```json
{ "scanners": { "fib_15m": { "enabled": true } },
  "schedule": { "intraday_every_min": 30 },
  "replay": { "universe_override": "nifty200" } }
```

## How a pick is graded

- **Daily picks**: entry at the next session's open; returns at +1/3/5/10/20
  sessions, excess vs Nifty, max favourable/adverse move in the first 5 sessions.
- **Intraday picks**: entry at the first 5-min bar after the scan; returns at
  +30 min, +60 min and the close.
- **Uniform plan (the fair comparison)**: the same rules for every scanner —
  daily stop 1.5×ATR / target 3×ATR / exit after 10 bars; intraday stop 0.25×
  median day range / target 2R / exit at close.
- **Native plan**: the scanner's own entry/stop/TP1 (and time stop for swing).
  Trigger entries that never fill are `no_fill`, not losses.
- Stop wins when a bar touches both levels; a gap through the stop exits at the open.

**De-duplication**: a daily pick counts once when it enters a bucket, and again
only if still there after `cooldown_days` (20). Intraday: once per session.

**Edge score** = uniform expectancy (R) × n/(n+30). `EDGE` needs ≥ 20 trades,
edge score > 0.08 and t-stat > 2.

## Caveats

- Replay uses today's index members (survivorship bias) and today's price
  adjustments. Trust live results once a bucket has 20+ live trades.
- Intraday scanners cannot be replayed; they build a track record from live runs only.
  yfinance keeps ~60 days of 5-min bars, so validation must run within that window
  (the scheduler does it the same day).
- yfinance NSE intraday data is ~15 minutes delayed.

## Files

`agent.db` (SQLite: runs, signals, outcomes), `agent.log`, `state.json`,
`cache/daily/*.pkl` (price cache), `reports/` — all ignored by git.
