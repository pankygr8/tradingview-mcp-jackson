"""
agent.py — the scanner agent: run → record → validate → compare, on a clock.

Commands (run from the dashboard folder):
  python -m scanner_agent scan     [--group daily|intraday|all] [--only ath,swing]
  python -m scanner_agent validate
  python -m scanner_agent report
  python -m scanner_agent replay   [--days 365] [--step 5] [--universe nifty100] [--only ath,swing]
  python -m scanner_agent cycle    # do whatever is due now (for Windows Task Scheduler, every 15 min)
  python -m scanner_agent daemon   # same as cycle, in a loop (leave a window open)
  python -m scanner_agent status   # what is stored, what ran last
  python -m scanner_agent picks    # print the latest session's ranked picks

Default day (IST, weekdays, NSE open):
  09:35 → 14:45  intraday scanners every 15 min
  15:50          daily scanners (ATH, swing, fib 1D, gann fan 1D)
  16:30          validate all open signals
  16:45          rebuild leaderboard + today's plan
  08:45          validate again (catches the previous day's daily signals' first bar)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime

from . import config, report, store, validator
from .adapters import ADAPTERS, ist_now, trading_session

STATE_PATH = os.path.join(config.AGENT_DIR, "state.json")


# ── logging ──────────────────────────────────────────────────────────────────

def _logger() -> logging.Logger:
    # Windows consoles default to cp1252, which cannot print the arrows/× used here
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    lg = logging.getLogger("scanner_agent")
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        lg.addHandler(fh)
        lg.addHandler(sh)
    return lg


LOG = _logger()
log = LOG.info


def _state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)


# ── scan ─────────────────────────────────────────────────────────────────────

def scan(group: str = "all", only: list[str] | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    con = store.connect()
    out = {}
    for name, sc in cfg["scanners"].items():
        if only and name not in only:
            continue
        if not only and (not sc.get("enabled") or (group != "all" and sc.get("group") != group)):
            continue
        ad = ADAPTERS.get(name)
        if not ad:
            log(f"scan: unknown scanner '{name}' in config — skipped")
            continue
        uni = sc.get("universe")
        now = ist_now()
        session = trading_session(now)
        run_id = store.start_run(con, name, uni, "live", session)
        t0 = time.time()
        try:
            payload = ad.run(uni, cfg["max_scan_wait_sec"])
            recs, buckets = ad.normalize(payload, {"universe": uni, "session": session,
                                                    "ts": now.isoformat(timespec="seconds")})
            n = store.add_signals(con, run_id, "live", recs, buckets, name, cfg["cooldown_days"])
            store.finish_run(con, run_id, "done", len(recs), n)
            log(f"scan: {name:<12} {uni:<9} {len(recs):>3} in buckets, {n:>3} new  ({time.time() - t0:.0f}s)")
            out[name] = {"in_buckets": len(recs), "new": n}
        except Exception as e:
            store.finish_run(con, run_id, "error", error=f"{type(e).__name__}: {e}")
            log(f"scan: {name} FAILED — {type(e).__name__}: {e}")
            LOG.debug(traceback.format_exc())
            out[name] = {"error": str(e)}
    return out


# ── clock logic ──────────────────────────────────────────────────────────────

def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _at_or_after(now: datetime, hhmm: str) -> bool:
    return (now.hour, now.minute) >= _hm(hhmm)


def market_open_today(st: dict, cfg: dict) -> bool:
    """Weekday + Nifty printed a bar today (catches exchange holidays). Cached per day."""
    now = ist_now()
    if now.weekday() >= 5:
        return False
    key = str(now.date())
    if st.get("open_check", {}).get("date") == key and st["open_check"].get("checked_after_open"):
        return st["open_check"]["open"]
    if (now.hour, now.minute) < (9, 20):
        return True  # can't know yet; assume open
    try:
        import yfinance as yf
        df = yf.Ticker(cfg["benchmark"]).history(period="5d", interval="5m")
        is_open = bool(df is not None and not df.empty and
                       df.index[-1].tz_convert("Asia/Kolkata").date() == now.date())
    except Exception:
        is_open = True   # network hiccup — don't skip the day on a guess
    st["open_check"] = {"date": key, "open": is_open, "checked_after_open": True}
    return is_open


def cycle(cfg: dict | None = None) -> list[str]:
    """Run whatever is due right now. Safe to call as often as you like."""
    cfg = cfg or config.load()
    sch = cfg["schedule"]
    st = _state()
    now = ist_now()
    today = str(now.date())
    did = []
    weekday = now.weekday() < 5
    is_open = market_open_today(st, cfg) if weekday else False

    # intraday scanners
    if is_open and _at_or_after(now, sch["intraday_start"]) and not _at_or_after(now, _plus(sch["intraday_end"], 1)):
        last = st.get("last_intraday")
        due = not last or (now - datetime.fromisoformat(last)).total_seconds() >= sch["intraday_every_min"] * 60 - 30
        if due:
            st["last_intraday"] = now.isoformat(timespec="seconds")
            _save_state(st)
            scan("intraday", cfg=cfg)
            did.append("intraday-scan")

    # daily scanners
    if is_open and _at_or_after(now, sch["daily_scan_at"]) and st.get("last_daily") != today:
        st["last_daily"] = today
        _save_state(st)
        scan("daily", cfg=cfg)
        did.append("daily-scan")

    # validation (several slots a day; any weekday incl. the morning after)
    for slot in sch["validate_at"]:
        key = f"{today} {slot}"
        if weekday and _at_or_after(now, slot) and key not in st.get("validated", []):
            st.setdefault("validated", []).append(key)
            st["validated"] = st["validated"][-20:]
            _save_state(st)
            validator.validate(log=log, cfg=cfg)
            did.append("validate")

    if weekday and _at_or_after(now, sch["report_at"]) and st.get("last_report") != today:
        st["last_report"] = today
        _save_state(st)
        report.render(cfg=cfg, log=log)
        did.append("report")

    _save_state(st)
    return did


def _plus(hhmm: str, minutes: int) -> str:
    h, m = _hm(hhmm)
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def daemon(cfg: dict | None = None) -> None:
    log("daemon: started — Ctrl+C to stop")
    while True:
        try:
            did = cycle(cfg or config.load())
            if did:
                log(f"daemon: did {', '.join(did)}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"daemon: cycle error {type(e).__name__}: {e}")
        time.sleep(60)


# ── status / picks ───────────────────────────────────────────────────────────

def status() -> None:
    con = store.connect()
    print("\nSignals stored")
    for r in con.execute("SELECT source, scanner, bucket, COUNT(*) n, MIN(session) a, MAX(session) b "
                         "FROM signals GROUP BY 1,2,3 ORDER BY 1,2,3"):
        print(f"  {r['source']:<7} {r['scanner']:<12} {r['bucket']:<16} {r['n']:>5}   {r['a']} → {r['b']}")
    print("\nOutcomes")
    for r in con.execute("SELECT COALESCE(o.status,'ungraded') st, COUNT(*) n FROM signals s "
                         "LEFT JOIN outcomes o ON o.signal_id=s.id GROUP BY 1"):
        print(f"  {r['st']:<10} {r['n']}")
    print("\nLast live runs")
    for r in con.execute("SELECT scanner, MAX(started_at) t, status, n_results, n_new, error FROM runs "
                         "WHERE source='live' GROUP BY scanner ORDER BY t DESC"):
        print(f"  {r['scanner']:<12} {r['t']}  {r['status']:<6} in={r['n_results']} new={r['n_new']} {r['error'] or ''}")
    print(f"\nState: {json.dumps(_state())}\n")


def picks() -> None:
    cfg = config.load()
    df = report.load_frame()
    if df.empty:
        print("no signals yet")
        return
    lb = report.leaderboard(df, cfg)
    plan = report.todays_plan(df, lb, cfg)
    if plan.empty:
        print("no live signals yet")
        return
    import pandas as pd
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(plan.head(40).to_string(index=False))


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="scanner_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--group", default="all", choices=["all", "daily", "intraday"])
    p.add_argument("--only", default="")
    sub.add_parser("validate")
    sub.add_parser("report")
    p = sub.add_parser("replay")
    p.add_argument("--days", type=int)
    p.add_argument("--step", type=int)
    p.add_argument("--universe")
    p.add_argument("--only", default="")
    p.add_argument("--keep", action="store_true", help="append instead of replacing old replay signals")
    sub.add_parser("cycle")
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("picks")
    a = ap.parse_args(argv)
    only = [x.strip() for x in getattr(a, "only", "").split(",") if x.strip()] or None

    if a.cmd == "scan":
        scan(a.group, only)
    elif a.cmd == "validate":
        validator.validate(log=log)
    elif a.cmd == "report":
        report.render(log=log)
    elif a.cmd == "replay":
        from . import replay as rp
        res = rp.replay(only, a.days, a.step, a.universe, fresh=not a.keep, log=log)
        log(f"replay: stored {res['signals']} signals over {res['dates']} dates — validating")
        validator.validate(log=log)
        report.render(log=log)
    elif a.cmd == "cycle":
        did = cycle()
        log(f"cycle: {', '.join(did) if did else 'nothing due'}")
    elif a.cmd == "daemon":
        daemon()
    elif a.cmd == "status":
        status()
    elif a.cmd == "picks":
        picks()
