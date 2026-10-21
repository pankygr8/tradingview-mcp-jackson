"""
store.py — SQLite persistence for the scanner agent.

Tables
  runs         one row per scanner execution (live or replay)
  signals      one row per NEW pick (after de-duplication), with the trade
               geometry known at signal time — never edited afterwards
  last_members previous run's bucket membership, used for "fresh entry" dedupe
  outcomes     validation results per signal (re-computed until complete)

Signals and outcomes are kept apart on purpose: measurement never mutates
the evidence it is measuring.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanner TEXT, universe TEXT, source TEXT,
    asof TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, n_results INTEGER, n_new INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    source TEXT,              -- live | replay
    scanner TEXT, bucket TEXT, universe TEXT,
    symbol TEXT, name TEXT,
    side TEXT,                -- long | short
    horizon TEXT,             -- daily | intraday
    signal_ts TEXT,           -- ISO, IST
    session TEXT,             -- YYYY-MM-DD (IST trading date of the signal)
    ref_price REAL,
    entry REAL, stop REAL, tp1 REAL, tp2 REAL,
    entry_type TEXT,          -- market | trigger
    time_budget INTEGER,      -- bars (daily) the native plan allows
    score REAL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_key ON signals(scanner, bucket, symbol, source, session);
CREATE TABLE IF NOT EXISTS last_members (
    scanner TEXT, bucket TEXT, source TEXT, symbols TEXT, asof TEXT,
    PRIMARY KEY (scanner, bucket, source)
);
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id INTEGER PRIMARY KEY,
    status TEXT,              -- pending | partial | done | no_data
    updated_at TEXT,
    metrics TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ── runs ─────────────────────────────────────────────────────────────────────

def start_run(con, scanner: str, universe: str, source: str, asof: str) -> int:
    cur = con.execute(
        "INSERT INTO runs(scanner, universe, source, asof, started_at, status) "
        "VALUES (?,?,?,?,?, 'running')",
        (scanner, universe, source, asof, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id: int, status: str, n_results: int = 0,
               n_new: int = 0, error: str | None = None) -> None:
    con.execute(
        "UPDATE runs SET finished_at=?, status=?, n_results=?, n_new=?, error=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), status, n_results, n_new,
         (error or "")[:500], run_id))
    con.commit()


# ── signals with de-duplication ──────────────────────────────────────────────

def _prev_members(con, scanner, bucket, source) -> set[str]:
    row = con.execute("SELECT symbols FROM last_members WHERE scanner=? AND bucket=? AND source=?",
                      (scanner, bucket, source)).fetchone()
    return set(json.loads(row["symbols"])) if row else set()


def _last_emit(con, scanner, bucket, symbol, source) -> str | None:
    row = con.execute("SELECT MAX(session) AS s FROM signals "
                      "WHERE scanner=? AND bucket=? AND symbol=? AND source=?",
                      (scanner, bucket, symbol, source)).fetchone()
    return row["s"] if row else None


def add_signals(con, run_id: int, source: str, records: list[dict],
                buckets_evaluated: list[str], scanner: str, cooldown_days: int) -> int:
    """
    Insert the records that qualify as NEW signals. Returns count inserted.

    intraday: at most one signal per scanner/bucket/symbol per session.
    daily:    new if the symbol was not in this bucket on the previous run,
              or if its last signal in this bucket is >= cooldown_days old.
    """
    prev = {b: _prev_members(con, scanner, b, source) for b in buckets_evaluated}
    now_members: dict[str, set] = {b: set() for b in buckets_evaluated}
    n = 0
    for r in records:
        b, sym = r["bucket"], r["symbol"]
        now_members.setdefault(b, set()).add(sym)
        last = _last_emit(con, scanner, b, sym, source)
        if last == r["session"]:
            continue
        if r["horizon"] == "daily" and sym in prev.get(b, set()) and last:
            age = (date.fromisoformat(r["session"]) - date.fromisoformat(last)).days
            if age < cooldown_days:
                continue
        con.execute(
            "INSERT INTO signals(run_id, source, scanner, bucket, universe, symbol, name, side,"
            " horizon, signal_ts, session, ref_price, entry, stop, tp1, tp2, entry_type,"
            " time_budget, score, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, source, scanner, b, r.get("universe"), sym, r.get("name"), r["side"],
             r["horizon"], r["signal_ts"], r["session"], r.get("ref_price"),
             r.get("entry"), r.get("stop"), r.get("tp1"), r.get("tp2"),
             r.get("entry_type", "market"), r.get("time_budget"), r.get("score"),
             json.dumps(r.get("meta", {}), default=str)))
        n += 1
    asof = records[0]["session"] if records else None
    for b, syms in now_members.items():
        con.execute("INSERT OR REPLACE INTO last_members(scanner, bucket, source, symbols, asof)"
                    " VALUES (?,?,?,?,?)", (scanner, b, source, json.dumps(sorted(syms)), asof))
    con.commit()
    return n


# ── outcomes ─────────────────────────────────────────────────────────────────

def pending_signals(con, include_done: bool = False) -> list[sqlite3.Row]:
    q = ("SELECT s.* FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id "
         "WHERE o.status IS NULL OR o.status IN ('pending','partial')")
    if include_done:
        q = "SELECT s.* FROM signals s"
    return con.execute(q + " ORDER BY s.symbol, s.signal_ts").fetchall()


def save_outcome(con, signal_id: int, status: str, metrics: dict) -> None:
    con.execute("INSERT OR REPLACE INTO outcomes(signal_id, status, updated_at, metrics)"
                " VALUES (?,?,?,?)",
                (signal_id, status, datetime.now().isoformat(timespec="seconds"),
                 json.dumps(metrics, default=float)))


def clear_source(con, source: str) -> None:
    ids = [r[0] for r in con.execute("SELECT id FROM signals WHERE source=?", (source,))]
    if ids:
        con.executemany("DELETE FROM outcomes WHERE signal_id=?", [(i,) for i in ids])
    con.execute("DELETE FROM signals WHERE source=?", (source,))
    con.execute("DELETE FROM last_members WHERE source=?", (source,))
    con.execute("DELETE FROM runs WHERE source=?", (source,))
    con.commit()
