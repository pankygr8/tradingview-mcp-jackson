"""
report.py — turn graded signals into a scanner leaderboard and today's plan.

Outputs (scanner_agent/reports/):
  leaderboard.html     self-contained page: leaderboard per horizon, live vs
                       replay, and today's picks ranked by their scanner's edge
  leaderboard.csv      the same leaderboard as data
  signals_graded.csv   every signal with its outcome, for your own analysis
  summary.md           plain-text digest the Claude agent reads

How scanners are ranked
  The cross-scanner yardstick is the UNIFORM plan expectancy in R — every
  signal traded with identical rules, so a scanner cannot look good just
  because its own stop/target happen to be generous. That expectancy is then
  shrunk toward zero by n/(n+k): 12 lucky trades should not outrank 300
  ordinary ones. A verdict is only given once n >= min_signals_for_verdict.
"""

from __future__ import annotations

import html
import json
import math
import os

import pandas as pd

from . import config, store
from .adapters import SCANNER_LABELS, ist_now


# ── data ─────────────────────────────────────────────────────────────────────

def load_frame(con=None) -> pd.DataFrame:
    con = con or store.connect()
    rows = con.execute(
        "SELECT s.*, o.status AS o_status, o.metrics AS o_metrics FROM signals s "
        "LEFT JOIN outcomes o ON o.signal_id = s.id").fetchall()
    recs = []
    for r in rows:
        d = dict(r)
        m = json.loads(d.pop("o_metrics") or "{}")
        d.update(m)
        recs.append(d)
    return pd.DataFrame(recs)


def _pf(rs: pd.Series) -> float | None:
    g, l = rs[rs > 0].sum(), -rs[rs < 0].sum()
    if l <= 0:
        return None if g <= 0 else float("inf")
    return round(float(g / l), 2)


def _tstat(x: pd.Series) -> float | None:
    x = x.dropna()
    if len(x) < 3 or x.std() == 0:
        return None
    return round(float(x.mean() / (x.std() / math.sqrt(len(x)))), 2)


def _plan_stats(g: pd.DataFrame, prefix: str) -> dict:
    col = f"{prefix}_outcome"
    if col not in g:
        return {}
    sub = g[g[col].notna()]
    traded = sub[sub[col].isin(["win", "loss", "time_exit"])]
    rcol = f"{prefix}_r"
    rs = traded[rcol].dropna().astype(float) if rcol in traded else pd.Series(dtype=float)
    nofill = int((sub[col] == "no_fill").sum())
    out = {
        f"{prefix}_trades": len(rs),
        f"{prefix}_win%": round(float((traded[col] == "win").mean() * 100), 1) if len(traded) else None,
        f"{prefix}_expR": round(float(rs.mean()), 3) if len(rs) else None,
        f"{prefix}_PF": _pf(rs) if len(rs) else None,
    }
    if prefix == "native":
        attempts = len(rs) + nofill
        out["native_fill%"] = round(len(rs) / attempts * 100, 1) if attempts else None
        out["native_invalid"] = int((sub[col] == "invalid").sum())
    return out


def leaderboard(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    ph = cfg["primary_daily_horizon"]
    k = cfg["shrinkage_k"]
    nmin = cfg["min_signals_for_verdict"]
    rows = []
    for (src, horizon, scanner, bucket), g in df.groupby(["source", "horizon", "scanner", "bucket"]):
        prim = f"ret_{ph}d" if horizon == "daily" else "ret_eod"
        xprim = "x" + prim
        r = g[prim].dropna().astype(float) if prim in g else pd.Series(dtype=float)
        row = {
            "source": src, "horizon": horizon, "scanner": scanner, "bucket": bucket,
            "label": SCANNER_LABELS.get(scanner, scanner),
            "signals": len(g),
            "graded": len(r),
            "primary": prim,
            "hit%": round(float((r > 0).mean() * 100), 1) if len(r) else None,
            "avg_ret%": round(float(r.mean()), 3) if len(r) else None,
            "median_ret%": round(float(r.median()), 3) if len(r) else None,
            "avg_excess%": round(float(g[xprim].dropna().mean()), 3) if xprim in g and g[xprim].notna().any() else None,
            "t_stat": _tstat(r),
            "avg_mfe%": round(float(g["mfe"].dropna().mean()), 3) if "mfe" in g and g["mfe"].notna().any() else None,
            "avg_mae%": round(float(g["mae"].dropna().mean()), 3) if "mae" in g and g["mae"].notna().any() else None,
        }
        if horizon == "daily":
            for h in cfg["daily_horizons"]:
                c = f"ret_{h}d"
                row[f"avg_{h}d%"] = round(float(g[c].dropna().mean()), 3) if c in g and g[c].notna().any() else None
        row.update(_plan_stats(g, "uni"))
        row.update(_plan_stats(g, "native"))
        n = row.get("uni_trades") or 0
        e = row.get("uni_expR")
        row["edge_score"] = round(e * n / (n + k), 3) if e is not None else None
        if n < nmin:
            row["verdict"] = f"too few ({n}/{nmin})"
        elif row["edge_score"] is not None and row["edge_score"] > 0.08 and (row["t_stat"] or 0) > 2:
            row["verdict"] = "EDGE"
        elif row["edge_score"] is not None and row["edge_score"] > 0:
            row["verdict"] = "weak edge"
        else:
            row["verdict"] = "no edge"
        rows.append(row)
    lb = pd.DataFrame(rows)
    return lb.sort_values(["source", "horizon", "edge_score"], ascending=[True, True, False],
                          na_position="last").reset_index(drop=True)


def todays_plan(df: pd.DataFrame, lb: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    live = df[df["source"] == "live"] if not df.empty else df
    if live.empty:
        return pd.DataFrame()
    last = live["session"].max()
    today = live[live["session"] == last].copy()
    # a bucket's track record: prefer live stats once they have a verdict, else replay
    edge = {}
    for _, r in lb.iterrows():
        key = (r["scanner"], r["bucket"])
        live_ready = r["source"] == "live" and (r.get("uni_trades") or 0) >= cfg["min_signals_for_verdict"]
        cur = edge.get(key)
        if cur is None or live_ready or (r["source"] == "replay" and cur["source"] == "live"
                                         and (cur.get("uni_trades") or 0) < cfg["min_signals_for_verdict"]):
            edge[key] = r
    today["edge_score"] = [edge.get((s, b), {}).get("edge_score") for s, b in zip(today.scanner, today.bucket)]
    today["verdict"] = [edge.get((s, b), {}).get("verdict", "no history") for s, b in zip(today.scanner, today.bucket)]
    today["hist_win%"] = [edge.get((s, b), {}).get("uni_win%") for s, b in zip(today.scanner, today.bucket)]
    today = today.sort_values(["edge_score", "score"], ascending=[False, False], na_position="last")
    cols = ["session", "signal_ts", "scanner", "bucket", "symbol", "name", "side", "ref_price",
            "entry", "stop", "tp1", "tp2", "entry_type", "score", "edge_score", "verdict", "hist_win%"]
    return today[[c for c in cols if c in today]]


# ── rendering ────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5b6472;--line:#e3e6eb;--pos:#127a4a;--neg:#b42318;--acc:#2754c5;--chip:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e7eaee;--mute:#98a2b3;--line:#262c35;--pos:#47c38a;--neg:#f27c70;--acc:#7da2ff;--chip:#1e2633}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--mute);margin:0 0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mute);font-weight:600;position:sticky;top:0;background:var(--card)}td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
.pos{color:var(--pos)}.neg{color:var(--neg)}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:12px}
.EDGE{background:var(--pos);color:#fff}.tabs{display:flex;gap:6px;margin:8px 0}.tabs button{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:5px 12px;border-radius:7px;cursor:pointer}
.tabs button.on{border-color:var(--acc);color:var(--acc);font-weight:600}.note{color:var(--mute);font-size:12.5px;max-width:900px}
.bar{display:inline-block;height:8px;border-radius:4px;vertical-align:middle;margin-left:6px}
"""


def _fmt(v, signed=False, pct=False):
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "–"
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        s = f"{v:+.2f}" if signed else f"{v:.2f}"
        return s + ("%" if pct else "")
    return html.escape(str(v))


def _cls(v):
    try:
        return "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
    except (TypeError, ValueError):
        return ""


def _lb_table(lb: pd.DataFrame, horizon: str, cfg: dict) -> str:
    sub = lb[lb["horizon"] == horizon]
    if sub.empty:
        return "<p class='note'>No signals yet.</p>"
    hz = [f"avg_{h}d%" for h in cfg["daily_horizons"]] if horizon == "daily" else []
    head = (["Scanner", "Bucket", "Signals", "Hit %", "Avg ret", "Excess vs Nifty", "t"] + [h.replace("avg_", "").replace("%", "") for h in hz]
            + ["MFE", "MAE", "Uniform trades", "Win %", "Exp R", "PF", "Native trades", "Fill %", "Native win %", "Native exp R", "Bad plans", "Edge score", "Verdict"])
    out = ["<table><tr>" + "".join(f"<th class='{'l' if i < 2 else ''}'>{h}</th>" for i, h in enumerate(head)) + "</tr>"]
    mx = max([abs(x) for x in sub["edge_score"].dropna()] + [0.01])
    for _, r in sub.iterrows():
        es = r.get("edge_score")
        bar = ""
        if es is not None and not pd.isna(es):
            w = int(abs(es) / mx * 60)
            bar = f"<span class='bar' style='width:{w}px;background:var(--{'pos' if es > 0 else 'neg'})'></span>"
        cells = [f"<td class='l'>{html.escape(r['label'])}</td>", f"<td class='l'><span class='chip'>{html.escape(r['bucket'])}</span></td>",
                 f"<td>{r['signals']}</td>", f"<td>{_fmt(r['hit%'])}</td>",
                 f"<td class='{_cls(r['avg_ret%'])}'>{_fmt(r['avg_ret%'], True, True)}</td>",
                 f"<td class='{_cls(r['avg_excess%'])}'>{_fmt(r['avg_excess%'], True, True)}</td>",
                 f"<td>{_fmt(r['t_stat'])}</td>"]
        cells += [f"<td class='{_cls(r.get(h))}'>{_fmt(r.get(h), True, True)}</td>" for h in hz]
        cells += [f"<td class='pos'>{_fmt(r['avg_mfe%'], True, True)}</td>", f"<td class='neg'>{_fmt(r['avg_mae%'], True, True)}</td>",
                  f"<td>{_fmt(r.get('uni_trades'))}</td>", f"<td>{_fmt(r.get('uni_win%'))}</td>",
                  f"<td class='{_cls(r.get('uni_expR'))}'>{_fmt(r.get('uni_expR'), True)}</td>", f"<td>{_fmt(r.get('uni_PF'))}</td>",
                  f"<td>{_fmt(r.get('native_trades'))}</td>", f"<td>{_fmt(r.get('native_fill%'))}</td>",
                  f"<td>{_fmt(r.get('native_win%'))}</td>",
                  f"<td class='{_cls(r.get('native_expR'))}'>{_fmt(r.get('native_expR'), True)}</td>",
                  f"<td class='{'neg' if (r.get('native_invalid') or 0) > 0 else ''}'>{_fmt(r.get('native_invalid'))}</td>",
                  f"<td class='{_cls(es)}'>{_fmt(es, True)}{bar}</td>",
                  f"<td><span class='chip {html.escape(str(r['verdict']).split()[0])}'>{html.escape(str(r['verdict']))}</span></td>"]
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out) + "</table>"


def _plan_table(plan: pd.DataFrame) -> str:
    if plan.empty:
        return "<p class='note'>No live signals yet — run <code>python -m scanner_agent scan</code> during market hours.</p>"
    head = ["Symbol", "Side", "Scanner", "Bucket", "Seen", "Entry", "Stop", "TP1", "Entry type", "Scanner score", "Bucket edge", "Hist win %", "Verdict"]
    out = ["<table><tr>" + "".join(f"<th class='{'l' if i < 4 else ''}'>{h}</th>" for i, h in enumerate(head)) + "</tr>"]
    for _, r in plan.head(60).iterrows():
        entry = r["entry"] if pd.notna(r.get("entry")) else None
        out.append("<tr>" + "".join([
            f"<td class='l'><b>{html.escape(str(r['symbol']).replace('.NS', ''))}</b> <span class='note'>{html.escape(str(r.get('name') or ''))}</span></td>",
            f"<td class='l {'pos' if r['side'] == 'long' else 'neg'}'>{r['side']}</td>",
            f"<td class='l'>{html.escape(SCANNER_LABELS.get(r['scanner'], r['scanner']).split(' (')[0])}</td>",
            f"<td class='l'><span class='chip'>{html.escape(r['bucket'])}</span></td>",
            f"<td>{_fmt(r['ref_price'])}</td>",
            f"<td>{_fmt(entry) if entry else 'next open'}</td>",
            f"<td>{_fmt(r['stop']) if pd.notna(r.get('stop')) else '1.5×ATR'}</td>",
            f"<td>{_fmt(r['tp1']) if pd.notna(r.get('tp1')) else '3×ATR'}</td>",
            f"<td>{r.get('entry_type') or ''}</td>", f"<td>{_fmt(r.get('score'))}</td>",
            f"<td class='{_cls(r.get('edge_score'))}'>{_fmt(r.get('edge_score'), True)}</td>",
            f"<td>{_fmt(r.get('hist_win%'))}</td>",
            f"<td><span class='chip {html.escape(str(r['verdict']).split()[0])}'>{html.escape(str(r['verdict']))}</span></td>",
        ]) + "</tr>")
    return "".join(out) + "</table>"


def render(cfg: dict | None = None, con=None, log=print) -> dict:
    cfg = cfg or config.load()
    df = load_frame(con)
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    if df.empty:
        log("report: no signals stored yet")
        lb = pd.DataFrame()
    else:
        lb = leaderboard(df, cfg)
        df.to_csv(os.path.join(config.REPORT_DIR, "signals_graded.csv"), index=False)
        lb.to_csv(os.path.join(config.REPORT_DIR, "leaderboard.csv"), index=False)
    plan = todays_plan(df, lb, cfg) if not df.empty else pd.DataFrame()
    if not plan.empty:
        plan.to_csv(os.path.join(config.REPORT_DIR, "todays_plan.csv"), index=False)

    sources = [s for s in ("live", "replay") if not lb.empty and s in set(lb["source"])]
    sections = []
    for s in sources:
        sl = lb[lb["source"] == s]
        sections.append(
            f"<div class='src' data-src='{s}'>"
            f"<h2>Daily / swing scanners — {s}</h2><p class='note'>Entry at next session open. Primary horizon "
            f"{cfg['primary_daily_horizon']} sessions. Uniform plan: stop {cfg['uniform_daily_plan']['stop_atr']}×ATR, "
            f"target {cfg['uniform_daily_plan']['target_atr']}×ATR, exit after {cfg['uniform_daily_plan']['max_bars']} bars.</p>"
            f"<div class='card'>{_lb_table(sl, 'daily', cfg)}</div>"
            f"<h2>Intraday scanners — {s}</h2><p class='note'>Entry at the first 5-min bar after the scan. Primary horizon: close. "
            f"Uniform plan: stop {cfg['uniform_intraday_plan']['stop_adr_frac']}× median daily range, target {cfg['uniform_intraday_plan']['target_r']}R, exit at close.</p>"
            f"<div class='card'>{_lb_table(sl, 'intraday', cfg)}</div></div>")
    tabs = "".join(f"<button data-t='{s}' class='{'on' if i == 0 else ''}'>{s.title()} signals</button>" for i, s in enumerate(sources))
    n_live = int((df["source"] == "live").sum()) if not df.empty else 0
    n_rep = int((df["source"] == "replay").sum()) if not df.empty else 0
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Scanner Leaderboard</title><style>{CSS}</style></head><body><main>
<nav style="margin-bottom:10px"><a href="index.html" style="color:var(--acc);font-weight:600;text-decoration:none;margin-right:14px">Leaderboard</a><a href="daily.html" style="color:var(--acc);font-weight:600;text-decoration:none">Daily results</a></nav>
<h1>Scanner leaderboard</h1>
<p class="sub">Updated {ist_now():%d %b %Y %H:%M} IST · {n_live} live signals · {n_rep} replay signals · page reloads every 5 min</p>
<h2>Today's trade plan — latest session's fresh picks, ranked by their bucket's track record</h2>
<div class="card">{_plan_table(plan)}</div>
<div class="tabs">{tabs}</div>
{''.join(sections) or "<p class='note'>No graded signals yet.</p>"}
<h2>How to read this</h2>
<p class="note"><b>Edge score</b> = uniform-plan expectancy (R per trade) × n/(n+{cfg['shrinkage_k']}); it is the one number that compares scanners
on identical trade rules. <b>EDGE</b> needs ≥{cfg['min_signals_for_verdict']} trades, edge score &gt; 0.08R and t-stat &gt; 2 on the primary return.
<b>Native</b> columns use each scanner's own entry/stop/TP1 (swing, intraday, momentum only). Stop-first is assumed when a bar touches both levels.
Replay results carry survivorship bias (today's index members) and should be confirmed by live results before sizing up.</p>
</main><script>
const b=[...document.querySelectorAll('.tabs button')],s=[...document.querySelectorAll('.src')];
function show(t){{b.forEach(x=>x.classList.toggle('on',x.dataset.t===t));s.forEach(x=>x.style.display=x.dataset.src===t?'':'none')}}
b.forEach(x=>x.onclick=()=>show(x.dataset.t));if(b.length)show(b[0].dataset.t);
</script></body></html>"""
    path = os.path.join(config.REPORT_DIR, "leaderboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)

    _summary_md(lb, plan, cfg, n_live, n_rep)
    log(f"report: {path}")
    return {"html": path, "rows": len(lb)}


def _summary_md(lb, plan, cfg, n_live, n_rep):
    lines = [f"# Scanner agent summary — {ist_now():%Y-%m-%d %H:%M} IST", "",
             f"Signals stored: {n_live} live, {n_rep} replay.", ""]
    if not lb.empty:
        cols = ["source", "horizon", "scanner", "bucket", "signals", "hit%", "avg_ret%", "avg_excess%", "t_stat",
                "uni_trades", "uni_win%", "uni_expR", "uni_PF", "native_trades", "native_win%", "native_expR",
                "edge_score", "verdict"]
        lines += ["## Leaderboard (sorted by edge score within source/horizon)", "",
                  lb[[c for c in cols if c in lb]].to_markdown(index=False) if _has_tabulate() else lb[[c for c in cols if c in lb]].to_string(index=False), ""]
    if not plan.empty:
        lines += ["## Latest session picks", "",
                  plan.head(40).to_markdown(index=False) if _has_tabulate() else plan.head(40).to_string(index=False)]
    with open(os.path.join(config.REPORT_DIR, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False
