"""page.py — gannsquare/index.html on the dashboard: today's Gann Square Blueprint scan of all NSE stocks."""

from __future__ import annotations

import html
import json
import os
import urllib.parse

import pandas as pd

from . import config, scan

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5b6472;--line:#e3e6eb;--pos:#127a4a;--neg:#b42318;--acc:#2754c5;--chip:#eef2fb;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--mute:#98a2b3;--line:#262c35;--pos:#47c38a;--neg:#f27c70;--acc:#7da2ff;--chip:#1e2633;--warn:#e0b04a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 6px;padding-top:10px;border-top:2px solid var(--line)}
h3{font-size:14px;margin:20px 0 6px}.sub,.note{color:var(--mute)}.note{font-size:12.5px;max-width:940px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600}td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}td.w{white-space:normal;min-width:260px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:12px}
.rec{background:var(--pos);color:#fff}.top{background:var(--neg);color:#fff}nav a{color:var(--acc);text-decoration:none;margin-right:14px;font-weight:600}
a{color:var(--acc)}a.tv{color:var(--ink);text-decoration:none;border-bottom:1px dotted var(--mute)}details summary{cursor:pointer;color:var(--acc);margin:6px 0}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:150px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--mute);font-size:12px}
.pt{max-width:900px}.pt td{padding:4px 10px}tr.d>td{padding:0 10px 6px;border-bottom:1px solid var(--line)}tr:has(+tr.d)>td{border-bottom:0}.pt td:first-child{color:var(--mute);text-align:left;width:150px}.pt td:last-child{text-align:left;white-space:normal}
input#q{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);width:260px;margin:6px 0}
"""
NAV = ('<nav><a href="../index.html">Leaderboard</a><a href="../daily.html">Daily results</a>'
       '<a href="../nifty200/index.html">Nifty 200</a><a href="index.html">Gann Square</a>'
       '<a href="../crypto/index.html">Crypto</a></nav>')


def _f(v, nd=2, signed=False):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if v != v:
        return "–"
    return f"{v:+,.{nd}f}" if signed else f"{v:,.{nd}f}"


def _e(s) -> str:
    return html.escape(str(s))


def _tv(sym: str) -> str:
    url = "https://www.tradingview.com/chart/?symbol=" + urllib.parse.quote(f"NSE:{sym}", safe="")
    return f"<a class='tv' href='{url}' target='_blank' rel='noopener'><b>{_e(sym)}</b></a>"


def _table(head, rows, left=(), cls=""):
    if not rows:
        return "<p class='note' style='padding:10px'>None today.</p>"
    h = "".join(f"<th class='{'l' if i in left else ''}'>{x}</th>" for i, x in enumerate(head))
    return f"<div class='card'><table class='{cls}'><tr>{h}</tr>{''.join(rows)}</table></div>"


def _pine_table(r) -> str:
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in r["table"].items())
    return f"<details><summary>Pine table — {_e(r['ticker'])}</summary><table class='pt'>{rows}</table></details>"


def _when(r):
    b = r.get("bars_to_square")
    if b is None:
        return "–"
    return "now" if b == 0 else f"in {b} bars" if b > 0 else f"{-b} bars ago"


def _on(r):
    parts = []
    if r["on_1x1"]:
        parts.append(f"1x1 {_f(r['level_1x1'])}")
    if r["on_target"]:
        parts.append(f"target {_f(r['nearest_target'])}")
    if r["on_angle"]:
        parts.append("swing angle")
    if r["on_level"]:
        parts.append(r["nearest_level_txt"])
    return ", ".join(parts) or "–"


def _sq_rows(rows, detail=True):
    out = []
    for r in rows:
        exp = f"<span class='chip {'top' if r['expect'] == 'TOP' else 'rec'}'>{r['expect']}</span>"
        star = f"★{r['conf']}" if r["conf"] >= 2 else str(r["conf"])
        time_ = "Ahead (strong)" if r["ahead"] else "Behind (weak)"
        dna = (("(prov) " if r["provisional"] else "") + f"{r['dna_dir']} {r['dna_start'][2:]} → {r['dna_end'][2:]}"
               f" · R {_f(r['R'])} · T {r['T']}")
        chg = r.get("chg_pct")
        out.append(
            f"<tr><td class='l'>{_tv(r['ticker'])}<br><span class='note'>{_e(r.get('name', ''))[:34]}</span></td>"
            f"<td>{_f(r['close'])}</td><td class='{'pos' if (chg or 0) > 0 else 'neg' if (chg or 0) < 0 else ''}'>{_f(chg, 2, True)}%</td>"
            f"<td class='l'>{exp}</td><td class='l'>{_e(r['next_square'])} · {_when(r)} (±{r['tol_bars']})</td>"
            f"<td class='l w'>{_e(_on(r))}</td><td>{star}</td><td class='l'>{time_}</td>"
            f"<td class='l'>{_e(dna)}</td><td>{r['rules_work']}/3</td><td>{_f(r.get('turnover_cr'), 1)}</td></tr>"
            + (f"<tr class='d'><td class='l' colspan='11'>{_pine_table(r)}</td></tr>" if detail else ""))
    return out


HEAD = ["Stock", "Close", "Day", "Expect", "Time square", "Price on", "Tools", "Price vs time",
        "DNA swing", "Rules work", "₹ cr/day"]


def render(cfg: dict, sync_info: dict | None = None) -> str:
    res = scan.load()
    rows = res.get("rows") or []
    sq = sorted([r for r in rows if r["square"]], key=lambda r: (-r["conf"], -r["rules_work"], r["ticker"]))
    due = sorted([r for r in rows if r["state"] == "due"], key=lambda r: (-r["rules_work"], r["ticker"]))
    soon = sorted([r for r in rows if not r["time_square"] and r.get("bars_to_square") is not None
                   and 0 < r["bars_to_square"] <= 5 and not r["sideways"] and not r["extending"]],
                  key=lambda r: (r["bars_to_square"], -r["rules_work"]))
    sync_path = os.path.join(config.STORE_DIR, "sync_status.json")
    if sync_info is None and os.path.isfile(sync_path):
        with open(sync_path) as f:
            sync_info = json.load(f)
    sync_info = sync_info or {}
    ready, uni = sync_info.get("ready", res.get("ready", 0)), sync_info.get("universe", res.get("universe", 0))
    boot = (f"<p class='note warn'>History backfill in progress: {ready} of {uni} stocks ready. "
            f"Stocks are added to the scan as their history completes.</p>") if uni and ready < uni * 0.98 else ""
    inp = res.get("inputs") or cfg["blueprint"]
    inputs = (f"{_e(inp['sw_mode'])}, reversal {inp['zz_mult']}×ATR · time tolerance {inp['tol_pct']}% of T · "
              f"provisional {'on' if inp['use_prov'] else 'off'} · sideways filter {'on' if inp['side_on'] else 'off'} · "
              f"angle scale {'auto (' + str(inp['ang_bars']) + ' bars)' if not inp['ang_scale'] else inp['ang_scale']}")

    hist_html = ""
    if os.path.isfile(scan.SQUARES):
        h = pd.read_csv(scan.SQUARES)
        h = h[h.date.isin(sorted(h.date.unique())[-10:])].sort_values(["date", "conf"], ascending=[False, False])
        hr = [f"<tr><td>{_e(x.date)}</td><td class='l'>{_tv(x.symbol)}</td><td>{_f(x.close)}</td>"
              f"<td class='l'>{_e(x.expect)}</td><td class='l'>{_e(x.time_square)}</td><td>{int(x.conf)}</td>"
              f"<td>{int(x.rules_work)}/3</td></tr>" for x in h.itertuples()]
        hist_html = ("<h2>Squares of the last 10 sessions</h2>"
                     + _table(["Date", "Stock", "Close", "Expect", "Time square", "Tools", "Rules work"], hr, (1, 3, 4)))

    all_rows = sorted(rows, key=lambda r: r["ticker"])
    ar = [f"<tr data-s='{_e(r['ticker'])} {_e(r.get('name', '')).lower()}'><td class='l'>{_tv(r['ticker'])}</td><td>{_f(r['close'])}</td>"
          f"<td class='l'>{_e(r['state'])}</td><td class='l'>{_e(r['move_dir'])} from {_f(r['move_start'])} ({r['move_bars']} bars)</td>"
          f"<td class='l'>{_e(r['next_square'] or '–')} {_when(r)}</td><td class='l'>{_e(r['nearest_level_txt'])}</td>"
          f"<td>{_f(r['level_1x1'])}</td><td>{r['rules_work']}/3</td></tr>" for r in all_rows]

    body = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gann Square Blueprint</title><style>{CSS}</style></head><body><main>
{NAV}
<h1>Gann Square Blueprint — NSE end-of-day screener</h1>
<p class="sub">Session {_e(res.get('data_date') or '–')} · {res.get('scanned', 0)} stocks scanned of {uni} NSE equities
 · prices from NSE's MCP server (bhavcopy, split/bonus adjusted) · updated {_e((res.get('scanned_at') or '')[:16].replace('T', ' '))} UTC</p>
{boot}
<div class="kpis"><div class="kpi"><b>{len(sq)}</b><span>PRICE-TIME SQUARE NOW</span></div>
<div class="kpi"><b>{len(due)}</b><span>time square due, price not on a level</span></div>
<div class="kpi"><b>{len(soon)}</b><span>time square in the next 1–5 bars</span></div>
<div class="kpi"><b>{sum(r['sideways'] for r in rows)}</b><span>sideways (skipped)</span></div></div>
<p class="note">Same rules as the TradingView indicator: a square is a time square (range/S or 0.5T–2T from the start of the
current move, within ±{inp['tol_pct']}% of T) while price sits on a level — a quarter of the DNA range, the 1x1 angle, a 0.5–2R target
or a swing angle — not while the DNA swing is still extending and not when the market is sideways. "Tools" counts how many of those
levels agree; "Rules work" is the indicator's history test on the stock's own swings (0–3). Settings: {inputs}.
Expect a turn, but enter only on a reversal candle with the stop beyond its extreme.</p>

<h2>PRICE-TIME SQUARE NOW</h2>
{_table(HEAD, _sq_rows(sq), (0, 3, 4, 5, 7, 8))}

<h2>Time square due — price not on a level yet (watch)</h2>
{_table(HEAD, _sq_rows(due), (0, 3, 4, 5, 7, 8))}

<h2>Time square in the next 1–5 bars</h2>
{_table(HEAD, _sq_rows(soon, detail=False), (0, 3, 4, 5, 7, 8))}
{hist_html}
<h2>All stocks</h2>
<input id="q" placeholder="Filter by symbol or name…" oninput="f(this.value)">
<details id="alld"><summary>Show all {len(all_rows)} stocks ▸</summary>
{_table(["Stock", "Close", "State", "Current move", "Next time square", "Nearest level", "1x1", "Rules work"], ar, (0, 2, 3, 4, 5), "all")}
</details>
<p class="note" style="margin-top:28px">Data: NSE bhavcopy via NSE's official MCP server, for information only — not investment advice.
Signals are not orders. Results JSON: <a href="results.json">results.json</a> · square history: <a href="squares.csv">squares.csv</a>.</p>
</main><script>
function f(q){{q=q.trim().toLowerCase();const d=document.getElementById('alld');if(q&&d)d.open=true;
document.querySelectorAll('table.all tr[data-s]').forEach(t=>{{t.style.display=!q||t.dataset.s.toLowerCase().includes(q)?'':'none'}})}}
</script></body></html>"""
    out = os.path.join(config.STATE_DIR, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)
    return out
