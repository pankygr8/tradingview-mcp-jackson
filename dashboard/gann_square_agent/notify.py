"""
notify.py — Telegram alerts for the Gann Square Blueprint EOD screener.

Same bot and GitHub secrets as the other agents (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
One alert per square, like the Pine alert: the key is the stock + start of the current
move + which time square, so a square that stays valid for a few sessions is sent once.
Sent keys are kept in state/alerts_sent.json.
"""

from __future__ import annotations

import json
import os

import requests

from . import config

SENT = os.path.join(config.STATE_DIR, "alerts_sent.json")
PAGE = os.environ.get("GANN_PAGE_URL", "https://pankygr8.github.io/tradingview-mcp-jackson/gannsquare/")


def send(text: str, log=print) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("alerts: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipped")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            log(f"alerts: Telegram returned {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"alerts: send failed — {type(e).__name__}: {e}")
        return False


def _line(r: dict) -> str:
    icon = "🔻" if r["expect"] == "TOP" else "🔺"
    star = f" ★{r['conf']}" if r["conf"] >= 2 else ""
    works = f" · history test {r['rules_work']}/3 rules work" if r["rules_work"] else ""
    return f"{icon} {r['alert']}{star}{works}"


def check(res: dict, cfg: dict, log=print) -> int:
    rows = res.get("rows") or []
    if not rows:
        return 0
    sent = set()
    if os.path.isfile(SENT):
        with open(SENT) as f:
            sent = set(json.load(f))
    new_sq = [r for r in rows if cfg["sq_alert"] and r.get("alert") and f"sq|{r['square_key']}" not in sent]
    new_w = [r for r in rows if cfg["watch_alert"] and r.get("watch_alert") and f"w|{r['square_key']}" not in sent]
    if not new_sq and not new_w:
        log("alerts: nothing new")
        return 0
    new_sq.sort(key=lambda r: (-r["conf"], -r["rules_work"], r["ticker"]))
    head = f"📐 Gann Square Blueprint · NSE EOD {res['data_date']}"
    lines = [_line(r) for r in new_sq] + [f"👀 {r['watch_alert']}" for r in new_w]
    per = max(1, cfg["alerts_per_message"])
    ok_all = True
    for i in range(0, len(lines), per):
        part = lines[i:i + per]
        title = (f"{head} — {len(new_sq)} price-time square(s)" + (f", {len(new_w)} watch" if new_w else "")
                 + (f" ({i // per + 1}/{(len(lines) - 1) // per + 1})" if len(lines) > per else ""))
        ok_all &= send(title + "\n\n" + "\n\n".join(part) + f"\n\n{PAGE}", log=log)
    if ok_all or not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        sent |= {f"sq|{r['square_key']}" for r in new_sq} | {f"w|{r['square_key']}" for r in new_w}
        with open(SENT, "w") as f:
            json.dump(sorted(sent)[-20000:], f)
    if ok_all:
        log(f"alerts: sent {len(new_sq)} square(s), {len(new_w)} watch")
    return len(new_sq)
