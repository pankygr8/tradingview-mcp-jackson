"""
notify.py — Telegram alerts for the Nifty 200 paper trades.

Uses the same GitHub secrets as the crypto agent (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID). One message per run, listing new signals and closed trades.
Each event is sent once (state/alerts_sent.json); the very first run records
existing events without sending them.
"""

from __future__ import annotations

import json
import os

import pandas as pd

from . import config, intraday, swing

SENT = os.path.join(config.STATE_DIR, "alerts_sent.json")
PAGE = os.environ.get("NIFTY_PAGE_URL", "https://pankygr8.github.io/tradingview-mcp-jackson/nifty200/")
IST = "Asia/Kolkata"


def _t(ts, fmt="%d %b %H:%M") -> str:
    try:
        t = pd.Timestamp(ts)
        return (t.tz_convert(IST) if t.tzinfo else t).strftime(fmt)
    except Exception:
        return str(ts)


def _n(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "–"


def send(text: str, log=print) -> bool:
    from crypto_agent.notify import send as _send   # same bot, same secrets
    return _send(text, log=log)


def _sym(s: str) -> str:
    return s.replace(".NS", "")


def check(itr: pd.DataFrame, swg: pd.DataFrame, log=print) -> None:
    first = not os.path.isfile(SENT)
    sent = set()
    if not first:
        with open(SENT) as f:
            sent = set(json.load(f))
    msgs, keys = [], []
    for _, t in (itr.iterrows() if not itr.empty else []):
        base = f"I|{t.strategy}|{t.target}|{t.sym}|{t.signal_ts}"
        if t.status in ("pending", "open") and base + "|new" not in sent:
            keys.append(base + "|new")
            arrow = "🟢 BUY" if t.side == "long" else "🔴 SELL"
            px = f"entry {_n(t.entry)}" if pd.notna(t.entry) else f"entry next 15-min open (last {_n(t['last'])})"
            tgt = "hold to 15:15" if t.target == "eod" else f"target {_n(t.target_px)}" if pd.notna(t.target_px) else f"target {t.target}"
            msgs.append(f"{arrow} {_sym(t.sym)} — intraday {intraday.LABEL.get(t.strategy, t.strategy)} ({t.target})\n"
                        f"signal {_t(t.signal_ts, '%H:%M')} · {px} · stop {_n(t.stop)} · {tgt} · qty≈{int(t.qty)}")
        if t.status == "closed" and base + "|closed" not in sent:
            keys.append(base + "|closed")
            msgs.append(f"{'✅' if t.R > 0 else '❌'} {_sym(t.sym)} intraday {t.strategy} ({t.target}) closed "
                        f"{t.outcome} {_n(t.entry)} → {_n(t.exit)} · {t.R:+.2f}R")
    for _, t in (swg.iterrows() if not swg.empty else []):
        base = f"S|{t.strategy}|{t.exit_rule}|{t.sym}|{t.signal_ts}"
        if t.status in ("pending", "open") and base + "|new" not in sent:
            keys.append(base + "|new")
            when = "buy at tomorrow's open" if t.status == "pending" else f"bought {_t(t.entry_ts, '%d %b')} at {_n(t.entry)}"
            msgs.append(f"🟢 SWING BUY {_sym(t.sym)} — {swing.LABEL.get(t.strategy, t.strategy)}\n"
                        f"signal close {_t(t.signal_ts, '%d %b')} · {when} · stop ≈{_n(t.stop)} · target ≈{_n(t.target_px)} "
                        f"· max 20 sessions · qty≈{int(t.qty)}")
        if t.status == "closed" and base + "|closed" not in sent:
            keys.append(base + "|closed")
            msgs.append(f"{'✅' if t.R > 0 else '❌'} SWING {_sym(t.sym)} {t.strategy} closed {t.outcome} "
                        f"{_n(t.entry)} → {_n(t.exit)} · {t.R:+.2f}R")
    if keys and not first:
        body = "\n\n".join(msgs[:25]) + (f"\n\n…and {len(msgs) - 25} more" if len(msgs) > 25 else "")
        if not send(("📊 Nifty 200 paper trades\n\n" + body + f"\n\n{PAGE}")[:4000], log=log):
            if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                return                           # configured but failed → retry next run
        else:
            log(f"alerts: sent {len(msgs)} event(s)")
    elif first and keys:
        log(f"alerts: first run — recorded {len(keys)} existing event(s) without sending")
    with open(SENT, "w") as f:
        json.dump(sorted(sent | set(keys)), f)
