"""
live.py — paper-trade every strategy on live Hyperliquid candles and publish a page.

Each run (every 15 min) re-simulates the last few UTC days from the candles —
the simulation is deterministic, so a trade's state (waiting / open / closed)
is simply recomputed and upserted. Closed trades are final; nothing is ever
sent to an exchange.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3

import pandas as pd

from . import config, data, strategies

IST = "Asia/Kolkata"

SCHEMA = """CREATE TABLE IF NOT EXISTS trades (
  key TEXT PRIMARY KEY, day TEXT, coin TEXT, strategy TEXT, btc_stop REAL,
  status TEXT, side TEXT, entry_ts TEXT, entry REAL, stop REAL, target REAL,
  exit_ts TEXT, exit REAL, outcome TEXT, net_pts REAL, R REAL, cost_pts REAL,
  target_pts REAL, stop_pts REAL, detail TEXT, updated TEXT)"""


def _con():
    con = sqlite3.connect(config.DB_PATH)
    con.execute(SCHEMA)
    return con


def update(cfg: dict | None = None, frames: dict | None = None, log=print) -> int:
    cfg = cfg or config.load()
    frames = frames or {c: data.history(c, cfg, live_only=True, log=log) for c in cfg["coins"]}
    btc = frames.get("BTC")
    btc_open = btc.groupby(btc.index.floor("D"))["Open"].first() if btc is not None and not btc.empty else None
    cost = cfg["fee_per_side"] + cfg["slippage_per_side"]
    con = _con()
    n = 0
    now = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    for coin, df in frames.items():
        if df.empty:
            continue
        all_days = sorted(set(df.index.floor("D")))
        days = all_days[-cfg["live_days"]:]
        for name in strategies.CATALOGUE:
            for stop in cfg["btc_stop_grid"]:
                for r in strategies.run(name, df, btc_open, coin, cfg["btc_target_pts"], stop, cost, days=days):
                    key = f"{r['day']:%Y-%m-%d}|{coin}|{name}|{int(stop)}"
                    old = con.execute("SELECT status FROM trades WHERE key=?", (key,)).fetchone()
                    if old and old[0] in ("closed", "expired"):
                        continue                       # final — never rewrite a closed trade
                    con.execute(
                        "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, f"{r['day']:%Y-%m-%d}", coin, name, stop, r["status"], r.get("side"),
                         str(r.get("entry_ts") or ""), r.get("entry"), r.get("stop"), r.get("target"),
                         str(r.get("exit_ts") or ""), r.get("exit"), r.get("outcome"), r.get("net_pts"),
                         r.get("R"), r.get("cost_pts"), r.get("target_pts"), r.get("stop_pts"),
                         json.dumps({k: v for k, v in r.items() if k in ("orders", "last", "unreal_pts")}, default=float),
                         now))
                    n += 1
    con.commit()
    last = {c: (float(f["Close"].iloc[-1]), f.index[-1]) for c, f in frames.items() if not f.empty}
    with open(os.path.join(config.STATE_DIR, "last_prices.json"), "w") as f:
        json.dump({c: [p, str(t)] for c, (p, t) in last.items()}, f)
    log(f"live: {n} paper-trade rows updated")
    return n


# ── page ─────────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5b6472;--line:#e3e6eb;--pos:#127a4a;--neg:#b42318;--acc:#2754c5;--chip:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--mute:#98a2b3;--line:#262c35;--pos:#47c38a;--neg:#f27c70;--acc:#7da2ff;--chip:#1e2633}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub,.note{color:var(--mute)}.note{font-size:12.5px;max-width:920px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600}td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
.pos{color:var(--pos)}.neg{color:var(--neg)}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:12px}
.rec{background:var(--pos);color:#fff}nav a{color:var(--acc);text-decoration:none;margin-right:14px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--mute);font-size:12px}
"""


def _f(v, nd=2, signed=False):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    return f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"


def _c(v):
    try:
        return "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
    except (TypeError, ValueError):
        return ""


def _ist(ts):
    if not ts:
        return "–"
    try:
        return pd.Timestamp(ts).tz_convert(IST).strftime("%d %b %H:%M")
    except Exception:
        return str(ts)


def _table(head, rows, left=1):
    h = "".join(f"<th class='{'l' if i < left else ''}'>{x}</th>" for i, x in enumerate(head))
    return f"<table><tr>{h}</tr>{''.join(rows)}</table>" if rows else "<p class='note' style='padding:10px'>Nothing yet.</p>"


def _summ(g: pd.DataFrame) -> dict:
    c = g[g.status == "closed"]
    return {"trades": len(c), "success": int((c.net_pts > 0).sum()), "failed": int((c.net_pts <= 0).sum()),
            "succ%": round((c.net_pts > 0).mean() * 100, 1) if len(c) else None,
            "R": round(float(c.R.sum()), 2) if len(c) else 0.0,
            "expR": round(float(c.R.mean()), 3) if len(c) else None}


def render(cfg: dict | None = None, log=print) -> str:
    cfg = cfg or config.load()
    con = _con()
    tr = pd.read_sql("SELECT * FROM trades", con)
    sel = {}
    if os.path.isfile(config.SELECTION_PATH):
        with open(config.SELECTION_PATH) as f:
            sel = json.load(f)
    rec = {(r["coin"], r["strategy"], float(r["btc_stop"])) for r in sel.get("recommended", [])}
    tr["rec"] = [(c, s, float(b)) in rec for c, s, b in zip(tr.coin, tr.strategy, tr.btc_stop)] if not tr.empty else []
    prices = {}
    p = os.path.join(config.STATE_DIR, "last_prices.json")
    if os.path.isfile(p):
        with open(p) as f:
            prices = json.load(f)
    bt_path = os.path.join(config.REPORT_DIR, "crypto_backtest.csv")
    bt = pd.read_csv(bt_path) if os.path.isfile(bt_path) else pd.DataFrame()

    # ── price strip ──
    kp = "".join(f"<div class='kpi'><b>{_f(v[0], 2)}</b><span>{c} · {_ist(v[1])} IST</span></div>" for c, v in prices.items())

    # ── today: recommended (or all, if nothing recommended yet) ──
    today_rows = []
    if not tr.empty:
        d = tr["day"].max()
        mid_stop = sorted(cfg["btc_stop_grid"])[len(cfg["btc_stop_grid"]) // 2]
        # recommended combos only; until there are any, one row per strategy (middle stop size)
        t = tr[(tr.day == d) & (tr.rec if rec else tr.btc_stop == mid_stop)]
        t = t.sort_values(["coin", "strategy", "btc_stop"])
        for _, r in t.iterrows():
            det = json.loads(r["detail"] or "{}")
            if r.status == "pending":
                lv = " / ".join(f"{s} {k} {_f(x)}" for s, k, x in det.get("orders", []))
                st, side, levels, pnl = "waiting for entry", "–", lv, "–"
            elif r.status == "open":
                st, side = "OPEN", r.side
                levels = f"entry {_f(r.entry)} · stop {_f(r.stop)} · target {_f(r.target)}"
                pnl = f"<span class='{_c(det.get('unreal_pts'))}'>{_f(det.get('unreal_pts'), 1, True)} pts</span>"
            elif r.status == "closed":
                st, side = r.outcome.replace("_", " "), r.side
                levels = f"{_f(r.entry)} → {_f(r.exit)}"
                pnl = f"<span class='{_c(r.net_pts)}'>{_f(r.net_pts, 1, True)} pts ({_f(r.R, 2, True)}R)</span>"
            else:
                st, side, levels, pnl = "no trade today", "–", "–", "–"
            today_rows.append(
                f"<tr><td class='l'><b>{r.coin}</b></td><td class='l'>{html.escape(r.strategy)}"
                f"{' <span class=\"chip rec\">recommended</span>' if r.rec else ''}</td>"
                f"<td>{int(r.btc_stop)}</td><td class='l'>{st}</td><td class='l {'pos' if side == 'long' else 'neg' if side == 'short' else ''}'>{side}</td>"
                f"<td class='l'>{levels}</td><td>{_ist(r.entry_ts) if r.entry_ts else '–'}</td><td>{pnl}</td></tr>")

    # ── live results by day ──
    day_rows = []
    if not tr.empty:
        scope = tr[tr.rec] if rec else tr
        for d, g in sorted(scope.groupby("day"), reverse=True):
            s = _summ(g)
            day_rows.append(f"<tr><td class='l'><b>{d}</b></td><td>{s['trades']}</td><td class='pos'>{s['success']}</td>"
                            f"<td class='neg'>{s['failed']}</td><td>{_f(s['succ%'], 1)}</td>"
                            f"<td class='{_c(s['R'])}'>{_f(s['R'], 2, True)}</td><td class='{_c(s['expR'])}'>{_f(s['expR'], 3, True)}</td></tr>")

    # ── live results per combo ──
    combo_rows = []
    if not tr.empty:
        rows = []
        for (c, s, b), g in tr.groupby(["coin", "strategy", "btc_stop"]):
            rows.append({"coin": c, "strategy": s, "btc_stop": b, "rec": bool(g.rec.iloc[0]), **_summ(g)})
        cr = pd.DataFrame(rows).sort_values(["rec", "R"], ascending=[False, False])
        for _, r in cr.head(40).iterrows():
            combo_rows.append(f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(r.strategy)}"
                              f"{' <span class=\"chip rec\">rec</span>' if r.rec else ''}</td><td>{int(r.btc_stop)}</td>"
                              f"<td>{r.trades}</td><td>{_f(r['succ%'], 1)}</td><td class='{_c(r.R)}'>{_f(r.R, 2, True)}</td>"
                              f"<td class='{_c(r.expR)}'>{_f(r.expR, 3, True)}</td></tr>")

    # ── backtest ──
    bt_rows = []
    if not bt.empty:
        for _, r in bt.head(30).iterrows():
            v = r["verdict"]
            bt_rows.append(
                f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(r.strategy)}</td><td>{int(r.btc_stop)}</td>"
                f"<td>1:{_f(r.rr, 2)}</td><td>{int(r.trades)}</td><td>{_f(r.trades_per_day, 2)}</td>"
                f"<td>{_f(r['target_hit%'], 1)}</td><td>{_f(r['success%'], 1)}</td>"
                f"<td class='{_c(r.exp_R)}'>{_f(r.exp_R, 3, True)}</td><td class='{_c(r.exp_R_h1)}'>{_f(r.exp_R_h1, 3, True)}</td>"
                f"<td class='{_c(r.exp_R_h2)}'>{_f(r.exp_R_h2, 3, True)}</td><td>{_f(r.PF, 2)}</td><td>{_f(r.max_dd_R, 1)}</td>"
                f"<td>{int(r.worst_streak) if pd.notna(r.worst_streak) else '–'}</td><td>{_f(r.avg_cost_pts, 1)}</td><td>{_f(r.t_stat, 2)}</td>"
                f"<td class='l'><span class='chip {'rec' if v == 'robust edge' else ''}'>{v}</span></td></tr>")
    span = bt["span"].iloc[0] if not bt.empty else ""
    no_rec = "" if rec else ("<p class='note'><b>No combination passed the robustness test yet</b> (profitable after fees in both halves "
                             "of the backtest). The table below shows every strategy instead — treat none of them as a trade plan.</p>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Crypto Paper Trading</title><style>{CSS}</style></head><body><main>
<nav><a href="../index.html">Leaderboard</a><a href="../daily.html">Daily results</a><a href="index.html">Crypto</a></nav>
<h1>BTC / ETH — paper trades</h1>
<p class="sub">Updated {pd.Timestamp.now(tz=IST):%d %b %Y %H:%M} IST · intraday target {cfg["btc_target_pts"]} BTC pts (ETH same %) ·
fees+slippage {(cfg['fee_per_side'] + cfg['slippage_per_side']) * 200:.2f}% round trip · day = UTC day (05:30 → 05:30 IST) · signals only, no orders sent</p>
<div class="kpis">{kp}</div>
{_swing_live()}
<h2>Intraday — today</h2>{no_rec}
<div class="card">{_table(["Coin", "Strategy", "Stop (BTC pts)", "Status", "Side", "Levels", "Entry time (IST)", "P&L"], today_rows, 6)}</div>
<h2>Live paper results by day{' — recommended combos' if rec else ' — all combos'}</h2>
<div class="card">{_table(["UTC day", "Trades", "Success", "Failed", "Success %", "Total R", "Avg R"], day_rows)}</div>
<h2>Live results per combo</h2>
<div class="card">{_table(["Coin", "Strategy", "Stop", "Trades", "Success %", "Total R", "Avg R"], combo_rows, 2)}</div>
<h2>Backtest {html.escape(str(span))}</h2>
<p class="note">Every strategy × stop size, fees included. <b>Robust edge</b> = profitable in BOTH halves of the test period with ≥ {cfg['min_trades']} trades;
t-stat ≥ {cfg.get('min_t_stat', 2.0)}; ranked by the weaker half. Target-hit % = reached +{cfg['btc_target_pts']} pts; success % = closed with net profit (includes profitable time exits).</p>
<div class="card">{_table(["Coin", "Strategy", "Stop", "R:R", "Trades", "Per day", "Target hit %", "Success %", "Exp R", "1st half", "2nd half",
                            "PF", "Max DD (R)", "Worst streak", "Cost pts", "t", "Verdict"], bt_rows, 2)}</div>
{_swing_section()}
{_levels_section()}
</main></body></html>"""
    path = os.path.join(config.REPORT_DIR, "crypto.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"crypto page: {path}")
    return path


def _levels_section() -> str:
    p = os.path.join(config.REPORT_DIR, "levels_backtest.csv")
    if not os.path.isfile(p):
        return ""
    d = pd.read_csv(p).head(20)
    rows = [f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(str(r.family))}</td><td class='l'>{r.rule}</td>"
            f"<td>{int(r.btc_stop)}</td><td>{int(r.trades)}</td><td>{_f(r.trades_per_month, 1)}</td><td>{_f(r['success%'], 1)}</td>"
            f"<td class='{_c(r.exp_R)}'>{_f(r.exp_R, 3, True)}</td><td class='{_c(r.exp_R_h1)}'>{_f(r.exp_R_h1, 3, True)}</td>"
            f"<td class='{_c(r.exp_R_h2)}'>{_f(r.exp_R_h2, 3, True)}</td><td>{_f(r.t_stat, 2)}</td>"
            f"<td class='l'><span class='chip'>{r.verdict}</span></td></tr>" for _, r in d.iterrows()]
    return ("<h2>Trade only at my level — price/time levels backtest " + html.escape(str(d['span'].iloc[0])) + "</h2>"
            "<p class='note'>Levels from confirmed 6-hour swings: trendlines, Gann 1×1 / 1×2 timing lines, 1.5× / 2× projections, "
            "pivot horizontals. Each level traded once, on its first touch (fade) or first retest after a break.</p>"
            "<div class='card'>" + _table(["Coin", "Level family", "Rule", "Stop", "Trades", "Per month", "Success %", "Exp R",
                                           "1st half", "2nd half", "t", "Verdict"], rows, 3) + "</div>")


def _swing_section() -> str:
    p = os.path.join(config.REPORT_DIR, "swing_backtest.csv")
    if not os.path.isfile(p):
        return ""
    d = pd.read_csv(p).head(25)
    rows = [f"<tr><td class='l'>{r.coin}</td><td class='l'>{html.escape(str(r.strategy))}</td><td>{int(r.btc_target)}</td>"
            f"<td>{int(r.btc_stop)}</td><td>{int(r.trades)}</td><td>{_f(r.trades_per_month, 1)}</td><td>{_f(r['success%'], 1)}</td>"
            f"<td class='{_c(r.exp_R)}'>{_f(r.exp_R, 3, True)}</td><td class='{_c(r.exp_R_h1)}'>{_f(r.exp_R_h1, 3, True)}</td>"
            f"<td class='{_c(r.exp_R_h2)}'>{_f(r.exp_R_h2, 3, True)}</td><td>{_f(r.t_stat, 2)}</td><td>{_f(r.avg_hold_d, 1)}</td>"
            f"<td class='l'><span class='chip {'rec' if r.verdict == 'robust edge' else ''}'>{r.verdict}</span></td></tr>"
            for _, r in d.iterrows()]
    return ("<h2>Multi-day swing backtest " + html.escape(str(d['span'].iloc[0])) + "</h2>"
            "<p class='note'>Targets 3000 / 5000 BTC points (ETH same %), stops 1500 / 2500, max 10-day hold, one position at a time "
            "per strategy, fees included. Robust edge = profitable in both halves, ≥ 20 trades, t ≥ 2.</p>"
            "<div class='card'>" + _table(["Coin", "Strategy", "Target", "Stop", "Trades", "Per month", "Success %", "Exp R",
                                           "1st half", "2nd half", "t", "Avg hold (days)", "Verdict"], rows, 2) + "</div>")


def _swing_live() -> str:
    try:
        from . import swing_live
        return swing_live.section()
    except Exception as e:
        return f"<p class='note'>Swing tracker unavailable: {html.escape(str(e))}</p>"
