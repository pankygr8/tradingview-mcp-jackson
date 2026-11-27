"""
data.py — NSE end-of-day prices for every NSE equity, from NSE's MCP server.

Store (store/, kept on the nse-eod-data branch between runs):
    universe.csv          symbol, name, series            (NSE EQUITY_L list)
    hist/<SYM>.csv.gz     raw bhavcopy rows: date, series, open, high, low, close, prev_close, volume, value
    actions/<SYM>.json    split / bonus / rights factors from get_corporate_actions
    meta.json             per symbol: oldest date fetched, backfill complete?, failures

Daily update (cheap): get_bulk_quote, 50 symbols per call → one new bar each.
  If the quote's previous close does not match the last stored close, a day was
  missed or a split/bonus went ex (NSE adjusts prevClose on the ex-date): the
  symbol is re-synced with the last 3 months + its corporate actions.
Backfill (first runs): get_stock_history in 3-month chunks back to history_years,
  resumable — each run continues where the last one stopped until its time budget.

Prices are stored raw and adjusted when read: price × product of the
adjustmentFactor of every action whose ex-date is after that bar (volume ÷ factor),
the same way TradingView shows split-adjusted daily charts.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from . import config
from .nse_mcp import MCPError, ToolError, client, pick, rows_of

HIST = os.path.join(config.STORE_DIR, "hist")
ACTS = os.path.join(config.STORE_DIR, "actions")
META = os.path.join(config.STORE_DIR, "meta.json")
UNIV = os.path.join(config.STORE_DIR, "universe.csv")
EQUITY_L = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
# Real-equity series only: the server sometimes returns a warrant (W*) / rights (N*, RE)
# row for a date instead of the EQ row — those prices belong to another instrument.
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "BL", "T0")
COLS = ["date", "series", "open", "high", "low", "close", "prev_close", "volume", "value"]
_lock = threading.Lock()


def _today() -> date:
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def _fname(sym: str) -> str:
    return sym.replace("&", "_AND_").replace("/", "_")


# ── universe ─────────────────────────────────────────────────────────────────
def universe(cfg: dict, log=print) -> pd.DataFrame:
    u = cfg["universe"]
    if isinstance(u, list):
        return pd.DataFrame({"symbol": [s.replace(".NS", "").upper() for s in u], "name": "", "series": "EQ"})
    if u != "all":
        import sys
        if config.DASHBOARD_DIR not in sys.path:
            sys.path.insert(0, config.DASHBOARD_DIR)
        import screener
        syms = screener.UNIVERSES[u]
        return pd.DataFrame({"symbol": [s.replace(".NS", "") for s in syms],
                             "name": [screener.NSE_STOCKS.get(s, "") for s in syms], "series": "EQ"})
    try:
        req = urllib.request.Request(EQUITY_L, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", "replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        df = pd.DataFrame([{"symbol": (x.get("SYMBOL") or "").strip().upper(),
                            "name": (x.get("NAME OF COMPANY") or "").strip(),
                            "series": (x.get(" SERIES") or x.get("SERIES") or "").strip().upper()} for x in rows])
        df = df[df.symbol.ne("") & df.series.isin(cfg["series"])].drop_duplicates("symbol")
        if len(df) > 500:
            df.to_csv(UNIV, index=False)
            log(f"universe: {len(df)} NSE equities from EQUITY_L ({', '.join(cfg['series'])})")
            return df
        log(f"universe: EQUITY_L looked wrong ({len(df)} rows) — using the saved list")
    except Exception as e:
        log(f"universe: EQUITY_L download failed ({type(e).__name__}: {e}) — using the saved list")
    if os.path.isfile(UNIV):
        return pd.read_csv(UNIV, keep_default_na=False)
    import sys
    if config.DASHBOARD_DIR not in sys.path:
        sys.path.insert(0, config.DASHBOARD_DIR)
    import screener
    log("universe: no saved list yet — falling back to the dashboard's NSE list")
    return pd.DataFrame({"symbol": [s.replace(".NS", "") for s in screener.NSE_STOCKS],
                         "name": list(screener.NSE_STOCKS.values()), "series": "EQ"})


# ── store helpers ────────────────────────────────────────────────────────────
def load_meta() -> dict:
    if os.path.isfile(META):
        with open(META) as f:
            return json.load(f)
    return {}


def save_meta(meta: dict) -> None:
    tmp = META + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, META)


def raw(sym: str) -> pd.DataFrame:
    p = os.path.join(HIST, _fname(sym) + ".csv.gz")
    if not os.path.isfile(p):
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(p, dtype={"date": str, "series": str})


def _write_raw(sym: str, df: pd.DataFrame) -> None:
    df = df[COLS].drop_duplicates(["date", "series"], keep="last").sort_values(["date", "series"])
    df.to_csv(os.path.join(HIST, _fname(sym) + ".csv.gz"), index=False, compression="gzip")


def _iso(v) -> str | None:
    """Any date the server sends (2026-11-27, 27-Nov-2026, 27-11-2026, epoch ms) → 'YYYY-MM-DD'."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return pd.Timestamp(v, unit="ms" if v > 1e11 else "s").strftime("%Y-%m-%d")
        t = str(v).strip()
        if len(t) >= 10 and t[4] == "-" and t[7] == "-":
            return t[:10]
        return pd.to_datetime(t, dayfirst=True).strftime("%Y-%m-%d")
    except (ValueError, TypeError, IndexError, OverflowError):
        return None


def _bar(r: dict, sym: str) -> dict | None:
    d = _iso(pick(r, "date", "tradeDate", "timestamp", default=""))
    if not d:
        return None
    try:
        return {"date": d, "series": str(pick(r, "series", default="EQ")).upper(),
                "open": float(pick(r, "open")), "high": float(pick(r, "high")),
                "low": float(pick(r, "low")), "close": float(pick(r, "close", "ltp", "lastPrice")),
                "prev_close": float(pick(r, "prevClose", "previousClose", "prev_close", default="nan")),
                "volume": float(pick(r, "volume", "totalTradedQuantity", "tradedQty", default=0)),
                "value": float(pick(r, "totalTradedValue", "tradedValue", "turnover", "value", default="nan"))}
    except (TypeError, ValueError):
        return None


def _merge(sym: str, bars: list[dict]) -> pd.DataFrame:
    old = raw(sym)
    new = pd.DataFrame(bars, columns=COLS)
    df = pd.concat([old, new], ignore_index=True) if not old.empty else new
    _write_raw(sym, df)
    return df


def actions(sym: str) -> list[dict]:
    p = os.path.join(ACTS, _fname(sym) + ".json")
    if os.path.isfile(p):
        with open(p) as f:
            return json.load(f)
    return []


def _fetch_actions(sym: str, start: str) -> None:
    payload = client().call("get_corporate_actions", {"symbol": sym, "fromDate": start,
                                                       "toDate": _today().isoformat()})
    rows = rows_of(payload)
    keep = {(a["ex_date"], a["purpose"]): a for a in actions(sym)}
    for a in rows:
        ex = str(pick(a, "exDate", "ex_date", default=""))[:10]
        try:
            fac = float(pick(a, "adjustmentFactor", "adjustment_factor", default=1.0))
        except (TypeError, ValueError):
            fac = 1.0
        if len(ex) == 10:
            purpose = str(pick(a, "purpose", "subject", "actionType", default=""))[:120]
            keep[(ex, purpose)] = {"ex_date": ex, "purpose": purpose, "factor": fac}
    with open(os.path.join(ACTS, _fname(sym) + ".json"), "w") as f:
        json.dump(sorted(keep.values(), key=lambda a: a["ex_date"]), f)


# ── NSE MCP fetches ──────────────────────────────────────────────────────────
def _history_chain(sym: str, end: str, target: str, deadline: float, max_chunks: int = 24):
    """get_stock_history backwards in 3-month chunks. Returns (bars, oldest_from, reached_end)."""
    bars, oldest, done = [], None, False
    for _ in range(max_chunks):
        if time.time() > deadline:
            break
        payload = client().call("get_stock_history", {"symbol": sym, "months": 3, "endDate": end})
        rows = rows_of(payload)
        got = [b for b in (_bar(r, sym) for r in rows) if b]
        bars += got
        frm = (payload.get("from_date") if isinstance(payload, dict) else None) or (min(b["date"] for b in got) if got else None)
        nxt = payload.get("next_end_date") if isinstance(payload, dict) else None
        if frm:
            oldest = frm if oldest is None else min(oldest, frm)
        if not got or not nxt or (frm and frm <= target):
            done = True
            break
        end = str(nxt)[:10]
    return bars, oldest, done


def _backfill(sym: str, meta: dict, target: str, deadline: float) -> str:
    m = meta.get(sym, {})
    old = raw(sym)
    try:
        if old.empty:
            bars, oldest, done = _history_chain(sym, "today", target, deadline)
            if not bars:
                return "empty"
        else:
            start = (pd.Timestamp(old["date"].min()) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            bars, oldest, done = _history_chain(sym, start, target, deadline)
        _merge(sym, bars)
        if done:
            _fetch_actions(sym, target)
        with _lock:
            m.update(oldest=oldest or m.get("oldest"), complete=bool(done), target=target, fails=0)
            meta[sym] = m
        return "complete" if done else "partial"
    except ToolError as e:
        with _lock:
            m.update(fails=m.get("fails", 0) + 1, error=str(e)[:160])
            if m["fails"] >= 3:
                m["complete"] = True           # unknown to the server — stop retrying
            meta[sym] = m
        return "tool-error"


def _resync(sym: str, meta: dict) -> bool:
    """Gap or corporate action: refetch the last 3 months and the corporate actions."""
    old = raw(sym)
    last = old["date"].max() if not old.empty else None
    payload = client().call("get_stock_history", {"symbol": sym, "months": 3, "endDate": "today"})
    bars = [b for b in (_bar(r, sym) for r in rows_of(payload)) if b]
    if not bars:
        return False
    if last and min(b["date"] for b in bars) > last:      # gap longer than 3 months → start over
        with _lock:
            meta[sym] = {"complete": False}
        _write_raw(sym, pd.DataFrame(bars, columns=COLS))
        return True
    _merge(sym, bars)
    start = (pd.Timestamp(last) - pd.Timedelta(days=10)).strftime("%Y-%m-%d") if last else \
        (_today() - timedelta(days=365 * 5)).isoformat()
    _fetch_actions(sym, start)
    return True


def _daily_update(syms: list[str], meta: dict, deadline: float, workers: int, log=print) -> int:
    """One new bar per symbol via get_bulk_quote; mismatches are re-synced."""
    last = {}
    for s in syms:
        r = raw(s)
        if not r.empty:
            r = r[r.series.isin(EQUITY_SERIES)]
            if not r.empty:
                row = r.sort_values("date").iloc[-1]
                last[s] = (row["date"], float(row["close"]))
    have = [s for s in syms if s in last]
    resync, added = [], 0
    chunks = [have[i:i + 50] for i in range(0, len(have), 50)]

    def one(chunk):
        payload = client().call("get_bulk_quote", {"symbols": chunk})
        return chunk, rows_of(payload)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, c) for c in chunks]
        for fu in as_completed(futs):
            try:
                chunk, rows = fu.result()
            except Exception as e:
                log(f"data: bulk quote failed ({type(e).__name__}: {e})")
                continue
            got = {}
            for r in rows:
                s = str(pick(r, "symbol", default="")).upper()
                b = _bar(r, s)
                if s and b:
                    got[s] = b
            if not got:
                resync += chunk                 # unexpected answer → fetch these the slow way
                continue
            for s in chunk:
                b = got.get(s)
                if not b or b["series"] not in EQUITY_SERIES:
                    continue
                d0, c0 = last[s]
                if b["date"] <= d0:
                    continue
                pc = b["prev_close"]
                if pc == pc and pc > 0 and abs(pc / c0 - 1) <= 0.0005:
                    _merge(s, [b])
                    added += 1
                else:
                    resync.append(s)
    if resync:
        log(f"data: re-syncing {len(resync)} symbol(s) (missed day or split/bonus/rights)")

        def rs(s):
            if time.time() > deadline:
                return False
            try:
                return _resync(s, meta)
            except MCPError:
                return False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            added += sum(bool(x) for x in pool.map(rs, resync))
    return added


def sync(cfg: dict, budget_min: float, log=print) -> dict:
    t0 = time.time()
    deadline = t0 + budget_min * 60
    uni = universe(cfg, log=log)
    syms = list(uni.symbol)
    meta = load_meta()
    target = (_today() - timedelta(days=int(365.25 * cfg["history_years"]))).isoformat()
    for s in syms:                                   # history_years raised → backfill further
        m = meta.get(s)
        if m and m.get("complete") and m.get("target", target) > target and m.get("fails", 0) < 3:
            m["complete"] = False

    added = _daily_update(syms, meta, deadline, cfg["workers"], log=log)
    save_meta(meta)
    log(f"data: daily update added {added} bar(s) in {time.time() - t0:.0f}s")

    todo = [s for s in syms if not meta.get(s, {}).get("complete")]
    todo.sort(key=lambda s: (s in meta, s))          # partly-filled first, then new
    done = {"complete": 0, "partial": 0, "empty": 0, "tool-error": 0, "error": 0, "not reached": 0}
    if todo:
        log(f"data: backfilling {len(todo)} symbol(s) to {target} (budget {max(0, deadline - time.time()) / 60:.0f} min)")

        def job(s):
            if time.time() > deadline:
                return "not reached"
            return _backfill(s, meta, target, deadline)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            futs = {pool.submit(job, s): s for s in todo}
            for fu in as_completed(futs):
                try:
                    done[fu.result()] += 1
                except Exception as e:
                    done["error"] += 1
                    log(f"data: {futs[fu]} failed ({type(e).__name__}: {str(e)[:120]})")
                n = sum(done.values()) - done["not reached"]
                if n and n % 100 == 0:
                    with _lock:
                        save_meta(meta)
                    log(f"data: backfill {n}/{len(todo)} ({time.time() - t0:.0f}s)")
    save_meta(meta)
    ready = sum(1 for s in syms if meta.get(s, {}).get("complete"))
    info = {"universe": len(syms), "ready": ready, "added_today": added, "backfill": done,
            "minutes": round((time.time() - t0) / 60, 1)}
    log(f"data: {ready}/{len(syms)} symbols have full history · {info}")
    with open(os.path.join(config.STORE_DIR, "sync_status.json"), "w") as f:
        json.dump({**info, "at": datetime.utcnow().isoformat() + "Z"}, f)
    return info


# ── read for the scan ────────────────────────────────────────────────────────
def adjusted(sym: str) -> pd.DataFrame:
    """Daily bars (oldest first), one row per date (EQ preferred), split/bonus adjusted."""
    df = raw(sym)
    if df.empty:
        return df
    df = df[df.series.isin(EQUITY_SERIES)].copy()
    rank = {s: i for i, s in enumerate(EQUITY_SERIES)}
    df["_r"] = df.series.map(rank)
    df = df.sort_values(["date", "_r"]).drop_duplicates("date", keep="first").drop(columns="_r")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)].reset_index(drop=True)
    acts = [a for a in actions(sym) if a.get("factor") and abs(float(a["factor"]) - 1) > 1e-4]
    if acts:
        f = pd.Series(1.0, index=df.index)
        for a in acts:
            f[df["date"] < a["ex_date"]] *= float(a["factor"])
        for c in ("open", "high", "low", "close", "prev_close"):
            df[c] = df[c] * f
        df["volume"] = df["volume"] / f
    return df
