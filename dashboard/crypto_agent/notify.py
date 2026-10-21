"""
notify.py — Telegram alerts when a swing paper trade opens or closes.

Set two GitHub repository secrets (Settings → Secrets and variables → Actions):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your chat id (see the setup steps in the README / chat)
The workflow passes them to this script as environment variables. Without
them, alerts are simply skipped (and logged).

Each event is sent once: sent keys are remembered in state/alerts_sent.json.
On the very first run, existing positions/trades are recorded WITHOUT sending,
so you are not flooded with old events.
"""

from __future__ import annotations

import json
import os

import pandas as pd
import requests

from . import config

SENT = os.path.join(config.STATE_DIR, "alerts_sent.json")
PAGE = os.environ.get("CRYPTO_PAGE_URL", "https://pankygr8.github.io/tradingview-mcp-jackson/crypto/")
IST = "Asia/Kolkata"


def _ist(ts) -> str:
    try:
        return pd.Timestamp(ts).tz_convert(IST).strftime("%d %b %H:%M IST")
    except Exception:
        return str(ts)


def _n(v) -> str:
    v = float(v)
    return f"{v:,.2f}" if v < 10000 else f"{v:,.0f}"


def send(text: str, log=print) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("alerts: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipped")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            log(f"alerts: Telegram returned {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"alerts: send failed — {type(e).__name__}: {e}")
        return False


def check(status: list[dict], trades: list[dict], log=print) -> None:
    first_run = not os.path.isfile(SENT)
    sent = set()
    if not first_run:
        with open(SENT) as f:
            sent = set(json.load(f))
    msgs, keys = [], []
    for s in status:
        p = s.get("position")
        if not p:
            continue
        k = f"open|{s['coin']}|{s['strategy']}|{s['btc_target']}|{s['btc_stop']}|{p['entry_ts']}"
        if k in sent:
            continue
        keys.append(k)
        icon = "🟢" if p["side"] == "long" else "🔴"
        msgs.append(f"{icon} NEW {p['side'].upper()} — {s['coin']} {s['strategy']} ({s['btc_target']}/{s['btc_stop']})\n"
                    f"Entry {_n(p['entry'])} at {_ist(p['entry_ts'])}\n"
                    f"Stop {_n(p['stop'])} · Target {_n(p['target'])}\n"
                    f"Time stop {_ist(p['time_stop'])}")
    for t in trades:
        if t.get("status") != "closed":
            continue
        k = f"closed|{t['coin']}|{t['strategy']}|{t['btc_target']}|{t['btc_stop']}|{t['entry_ts']}"
        if k in sent:
            continue
        keys.append(k)
        ok = t["net_pts"] > 0
        msgs.append(f"{'✅' if ok else '❌'} CLOSED {t['side'].upper()} — {t['coin']} {t['strategy']} "
                    f"({t['btc_target']}/{t['btc_stop']})\n"
                    f"{_n(t['entry'])} → {_n(t['exit'])} ({str(t['outcome']).replace('_', ' ')}) at {_ist(t['exit_ts'])}\n"
                    f"Net {t['net_pts']:+,.1f} pts · {t['R']:+.2f}R")
    if keys and not first_run:
        text = "📈 Swing paper trading\n\n" + "\n\n".join(msgs) + f"\n\n{PAGE}"
        if not send(text[:4000], log=log):
            # keep unsent events for the next run only if Telegram is configured but failed
            if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                return
        else:
            log(f"alerts: sent {len(msgs)} event(s)")
    elif first_run and keys:
        log(f"alerts: first run — recorded {len(keys)} existing event(s) without sending")
    with open(SENT, "w") as f:
        json.dump(sorted(sent | set(keys)), f)


def test(log=print) -> None:
    ok = send("✅ Test alert from your crypto swing agent.\n" + PAGE, log=log)
    log("alerts: test message sent" if ok else "alerts: test message NOT sent")
