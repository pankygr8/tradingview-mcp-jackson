"""
blueprint.py — Python port of the Pine v5 indicator "Gann Square Blueprint".

The logic follows the Pine script line by line (same inputs, defaults,
rounding, tie-breaks and texts) so a stock flagged here shows the same table
on TradingView. Only the drawing code is left out, plus "Manual: two points"
mode, which needs a person to pick the swing.

    DNA swing   last completed major swing: range R (price), duration T (bars), scale S = R / T
    1. price vs price        25/50/75/100 % levels of R (+ clones)
    2. price vs time         1x1 angle (S per bar) from the start of the current move
    3. range vs time         square boxes 0.5x .. 2x R reached at 0.5x .. 2x T
    4. range vs future time  current range / S, and 0.5T .. 2T from the move start
    5. swing angles          DNA swing angle, its 50 % and 25 % angles, first-touch levels, 25 % break zone
    PRICE-TIME SQUARE NOW = a time square is due within tolerance AND price is on a level (1/2/3/5),
                            not while the DNA swing is still extending, not when sideways.

evaluate(dates, high, low, close, **inputs) runs the indicator over the whole
history (oldest bar first) and returns what the Pine table shows on the last bar.
"""

from __future__ import annotations

import math

import numpy as np

DEFAULTS = {
    "sw_mode": "ATR zigzag (significant swings)",   # or "Pivot (N bars each side)"
    "zz_mult": 3.0,          # zigzag: reversal of at least N x ATR(14)
    "w": 10,                 # pivot mode: bars each side
    "use_prov": True,        # use the latest extreme when the current move is bigger than the DNA swing and has turned back
    "tol_pct": 10.0,         # time tolerance, % of T (min 1 bar)
    "ang_scale": 0.0,        # price per bar that makes 45 degrees (0 = auto)
    "ang_bars": 150,         # auto scale: bars on screen
    "side_on": True,         # no square signal when the last swings are small (sideways)
    "n_clone": 1,            # clone the range above / below (only widens the level search)
}
ZIGZAG = "ATR zigzag (significant swings)"
MAX_TURNS = 400


# ── Pine helpers ─────────────────────────────────────────────────────────────
def pine_round(x: float) -> int:
    """math.round: nearest integer, ties up (Python's round() is banker's rounding)."""
    return int(math.floor(x + 0.5))


def _num(x: float) -> str:
    """str.tostring(x) for a plain number: drops a trailing .0 (1.0 -> '1', 0.5 -> '0.5')."""
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _px(x: float) -> str:
    """str.tostring(x, format.mintick) — NSE ticks are 0.05 / 0.01, shown with 2 decimals."""
    return f"{x:.2f}"


def _fmt(x: float, pattern: str) -> str:
    if pattern == "#":
        return str(pine_round(x))
    if pattern == "#.#":
        return f"{math.floor(x * 10 + 0.5) / 10:.1f}".rstrip("0").rstrip(".")
    if pattern == "#.##":
        return f"{math.floor(x * 100 + 0.5) / 100:.2f}".rstrip("0").rstrip(".")
    if pattern == "#.###":
        return f"{math.floor(x * 1000 + 0.5) / 1000:.3f}".rstrip("0").rstrip(".")
    return str(x)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """ta.atr(n) = ta.rma(ta.tr(true), n): Wilder average, seeded with the SMA of the first n bars."""
    size = len(close)
    tr = np.empty(size)
    tr[0] = high[0] - low[0]
    if size > 1:
        pc = close[:-1]
        tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - pc), np.abs(low[1:] - pc)))
    out = np.full(size, np.nan)
    if size >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, size):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def _pivots(arr: np.ndarray, w: int, high: bool) -> np.ndarray:
    """ta.pivothigh / ta.pivotlow(src, w, w): value of bar i-w, reported on bar i."""
    size = len(arr)
    out = np.full(size, np.nan)
    for i in range(2 * w, size):
        c = i - w
        left, right, v = arr[c - w:c], arr[c + 1:i + 1], arr[c]
        if high:
            ok = v > left.max() and v >= right.max()
        else:
            ok = v < left.min() and v <= right.min()
        if ok:
            out[i] = v
    return out


# ── the indicator ────────────────────────────────────────────────────────────
def zigzag(high, low, close, sw_mode=ZIGZAG, zz_mult=3.0, w=10, atr14=None):
    """Major turns, alternating high (+1) / low (-1): ([bar], [price], [kind])."""
    zI: list[int] = []
    zP: list[float] = []
    zK: list[int] = []

    def f_add(i, p, k):
        n = len(zI)
        if n > 0 and zK[n - 1] == k:
            if (k == 1 and p > zP[n - 1]) or (k == -1 and p < zP[n - 1]):
                zI[n - 1] = i
                zP[n - 1] = p
        else:
            zI.append(i)
            zP.append(p)
            zK.append(k)

    size = len(close)
    atrZ = atr(high, low, close, 14) if atr14 is None else atr14
    pivot_mode = sw_mode != ZIGZAG
    if pivot_mode:
        ph, pl = _pivots(high, w, True), _pivots(low, w, False)
    zzDir = 0
    zzHiP = zzLoP = None
    zzHiI = zzLoI = None
    for b in range(size):
        hi, lo = high[b], low[b]
        if pivot_mode:
            if not np.isnan(ph[b]):
                f_add(b - w, float(ph[b]), 1)
            if not np.isnan(pl[b]):
                f_add(b - w, float(pl[b]), -1)
        elif not np.isnan(atrZ[b]):
            rev = zz_mult * atrZ[b]
            if zzDir == 0:
                if zzHiP is None or hi > zzHiP:
                    zzHiP, zzHiI = hi, b
                if zzLoP is None or lo < zzLoP:
                    zzLoP, zzLoI = lo, b
                if zzHiP - lo >= rev and zzHiI < b:
                    f_add(zzHiI, zzHiP, 1)
                    zzDir = -1
                    zzLoP, zzLoI = lo, b
                elif hi - zzLoP >= rev and zzLoI < b:
                    f_add(zzLoI, zzLoP, -1)
                    zzDir = 1
                    zzHiP, zzHiI = hi, b
            elif zzDir == 1:
                if hi > zzHiP:
                    zzHiP, zzHiI = hi, b
                elif zzHiP - lo >= rev:
                    f_add(zzHiI, zzHiP, 1)
                    zzDir = -1
                    zzLoP, zzLoI = lo, b
            else:
                if lo < zzLoP:
                    zzLoP, zzLoI = lo, b
                elif hi - zzLoP >= rev:
                    f_add(zzLoI, zzLoP, -1)
                    zzDir = 1
                    zzHiP, zzHiI = hi, b
        if len(zI) > MAX_TURNS:
            zI.pop(0)
            zP.pop(0)
            zK.pop(0)
    return zI, [float(p) for p in zP], zK


def _f_near(x, m, tol):
    return abs(x / m - 1) <= tol


def _f_rule(h, n, c, nc):
    p = c * 1.0 / nc if nc > 0 else None
    z = (h - n * p) / math.sqrt(n * p * (1 - p)) if (n > 0 and p is not None and 0 < p < 1) else None
    ok = z is not None and z >= 2.0 and h >= 5
    suffix = " - WORKS" if ok else " - maybe" if (z is not None and z >= 1.3) else " - no edge"
    pct = 100.0 * h / n if n > 0 else 0
    txt = (f"{h}/{n} ({_fmt(pct, '#')}%) vs chance {_fmt(0 if p is None else 100 * p, '#')}%" + suffix)
    return txt, ok, (None if z is None else round(z, 2))


def _history_test(zI, zP):
    """Each swing vs the swing before it, against chance (the same rule with a random other swing)."""
    nZ = len(zI)
    hT = hR = hD = nT = cT = cR = cD = nC = 0
    ks = (0.5, 1.0, 1.5, 2.0)
    if nZ >= 6:
        rng = [abs(zP[j] - zP[j - 1]) for j in range(nZ)]
        dur = [max(1, zI[j] - zI[j - 1]) for j in range(nZ)]
        for i in range(2, nZ):
            ri = abs(zP[i] - zP[i - 1])
            di = zI[i] - zI[i - 1]
            rp = abs(zP[i - 1] - zP[i - 2])
            dp = max(1, zI[i - 1] - zI[i - 2])
            sp = rp / dp
            nT += 1
            pred = ri / max(sp, 1e-10)
            hT += 1 if abs(di - pred) <= max(2.0, 0.15 * pred) else 0
            rr = ri / max(rp, 1e-10)
            hR += 1 if any(_f_near(rr, k, 0.1) for k in ks) else 0
            dd = di / dp
            hD += 1 if any(_f_near(dd, k, 0.1) for k in ks) else 0
            for j in range(2, nZ):
                if j != i and j != i - 1:
                    rq, dq = rng[j], dur[j]
                    sq_ = rq / dq
                    nC += 1
                    pr2 = ri / max(sq_, 1e-10)
                    cT += 1 if abs(di - pr2) <= max(2.0, 0.15 * pr2) else 0
                    r2 = ri / max(rq, 1e-10)
                    cR += 1 if any(_f_near(r2, k, 0.1) for k in ks) else 0
                    d2 = di / dq
                    cD += 1 if any(_f_near(d2, k, 0.1) for k in ks) else 0
    return _f_rule(hT, nT, cT, nC), _f_rule(hR, nT, cR, nC), _f_rule(hD, nT, cD, nC)


def evaluate(dates, high, low, close, ticker: str = "", timeframe: str = "D", **inputs) -> dict:
    """Run the indicator over the full history; return the last bar's table, flags and alert texts."""
    p = {**DEFAULTS, **{k: v for k, v in inputs.items() if v is not None}}
    H = np.asarray(high, dtype=float)
    L = np.asarray(low, dtype=float)
    C = np.asarray(close, dtype=float)
    D = list(dates)
    n = len(C)
    out: dict = {"ok": False, "ticker": ticker, "bars": n}
    if n < 20:
        out["message"] = "Not enough bars"
        return out
    atr14 = atr(H, L, C, 14)
    zI, zP, zK = zigzag(H, L, C, p["sw_mode"], p["zz_mult"], p["w"], atr14)
    nZ = len(zI)
    b = n - 1
    close_ = C[b]
    out.update(date=str(D[b])[:10], close=round(float(close_), 2), turns=nZ)
    if nZ < 2:
        out["message"] = "Not enough major turns - lower 'bars each side'"
        return out

    def hist(arr, k):          # Pine arr[k] on the last bar
        return arr[b - k]

    # ── DNA swing ───────────────────────────────────────────────────────────
    iA, pA, iB, pB = zI[nZ - 2], zP[nZ - 2], zI[nZ - 1], zP[nZ - 1]
    R = abs(pB - pA)
    T = max(1, iB - iA)
    S = R / T
    dnaUp = pB > pA
    dir_ = -1 if dnaUp else 1                      # the current move runs against the DNA swing
    i0, p0 = iB, pB
    el = b - i0

    def extreme(start_p, start_i, d, elapsed):
        pe, ie = start_p, start_i
        for k in range(elapsed + 1):
            v = hist(H, k) if d == 1 else hist(L, k)
            if (d == 1 and v > pe) or (d == -1 and v < pe):
                pe, ie = v, b - k
        return pe, ie

    pe, ie = extreme(p0, i0, dir_, el)
    r = abs(pe - p0)
    provisional = False
    if p["use_prov"] and r > R and ie < b:
        bk = pe
        for k in range(b - ie + 1):
            bk = min(bk, hist(L, k)) if dir_ == 1 else max(bk, hist(H, k))
        if abs(bk - pe) >= 0.236 * r:
            provisional = True
            iA, pA, iB, pB = i0, p0, ie, pe
            R = r
            T = max(1, ie - i0)
            S = R / T
            dnaUp = pB > pA
            dir_ = -1 if dnaUp else 1
            i0, p0 = iB, pB
            el = b - i0
            pe, ie = extreme(p0, i0, dir_, el)
            r = abs(pe - p0)
    extending = hist(L, 0) < p0 if dir_ == 1 else hist(H, 0) > p0
    atrV = atr14[b]
    if np.isnan(atrV):
        out["message"] = "ATR not ready"
        return out
    band = max(0.25 * atrV, 0.003 * close_)
    tolT = max(1, pine_round(p["tol_pct"] / 100.0 * T))

    # ── 1. price vs price: quarter levels of R ─────────────────────────────
    lo = min(pA, pB)
    q1Txt, q1Near, q1Best = "", None, 1.0e18
    nCl = min(8, max(p["n_clone"], int(math.ceil(max(lo - close_, close_ - (lo + R)) / max(R, 1e-10))) + 1))
    for q in range(-4 * nCl, 4 + 4 * nCl + 1):
        lv = lo + R * q / 4.0
        if lv > 0 and abs(close_ - lv) < q1Best:
            q1Best = abs(close_ - lv)
            q1Near = lv
            q1Txt = f"{q * 25}% level {_px(lv)}"
    onQ1 = q1Best <= band

    # ── 2. price vs time: 1x1 from the current move's start ────────────────
    a1 = p0 + dir_ * S * el
    ahead = close_ > a1 if dir_ == 1 else close_ < a1
    onA = abs(close_ - a1) <= band
    if extending:
        q2Txt = "DNA swing still extending (price beyond its end) - wait for a new turn"
    elif onA:
        q2Txt = "ON the 1x1 now: price and time SQUARED - trend change / new trend starts here"
    elif ahead:
        q2Txt = "price AHEAD of time (beyond 1x1): move is strong, " + ("don't sell" if dir_ == 1 else "don't buy")
    else:
        q2Txt = "price BEHIND time (inside 1x1): move is weak, a turn back is likely"

    # ── 3. range vs time: square boxes and targets ─────────────────────────
    q3Txt, q3Near, q3Best = "", None, 1.0e18
    targets = []
    for m in range(1, 5):
        f = m * 0.5
        tp = p0 + dir_ * f * R
        if tp > 0:
            q3Txt += (" | " if m > 1 else "") + f"{_num(f)}R {_px(tp)}"
            targets.append(round(tp, 2))
            if abs(close_ - tp) < q3Best:
                q3Best = abs(close_ - tp)
                q3Near = tp
    onQ3 = q3Best <= band

    # ── 4. range vs future time ────────────────────────────────────────────
    tR = i0 + int(pine_round(r / max(S, 1e-10)))
    tq = [tR] + [i0 + int(pine_round(m * 0.5 * T)) for m in range(1, 5)]
    tn = ["range/S"] + [f"{_num(m * 0.5)}T" for m in range(1, 5)]
    nxtB, nxtN, dMin = None, "", 100000
    for x, name in zip(tq, tn):
        d = x - b
        if d >= -tolT and abs(d) < abs(dMin):
            dMin, nxtB, nxtN = d, x, name
    timeSq = nxtB is not None and abs(dMin) <= tolT

    # ── 5. swing angles: DNA swing angle, 50 % and 25 % of it, from the swing start
    nG = min(p["ang_bars"], b)
    hiG, loG = float(hist(H, 0)), float(hist(L, 0))
    for k in range(nG + 1):
        hiG = max(hiG, hist(H, k))
        loG = min(loG, hist(L, k))
    u = p["ang_scale"] if p["ang_scale"] > 0 else 2.0 * (hiG - loG) / max(nG, 1)
    swS = (pB - pA) / T
    th = math.atan(abs(swS) / max(u, 1e-10))
    sg = 1.0 if pB > pA else -1.0
    s50 = sg * u * math.tan(th / 2)
    s25 = sg * u * math.tan(th / 4)
    deg = math.degrees(th)
    v50 = pA + s50 * (b - iA)
    v25 = pA + s25 * (b - iA)
    gDir = 1 if pB > pA else -1
    t50 = t25 = gZone = None
    jz = None
    if iB < b:
        for j in range(iB + 1, b + 1):
            L5 = pA + s50 * (j - iA)
            L2 = pA + s25 * (j - iA)
            if t50 is None and ((gDir == 1 and L[j] <= L5) or (gDir == -1 and H[j] >= L5)):
                t50 = L5
            if t25 is None and ((gDir == 1 and L[j] <= L2) or (gDir == -1 and H[j] >= L2)):
                t25 = L2
            if gZone is None and ((gDir == 1 and C[j] < L2) or (gDir == -1 and C[j] > L2)):
                gZone, jz = L2, j
    onG50 = abs(close_ - v50) <= band or (t50 is not None and abs(close_ - t50) <= band)
    onG25 = (abs(close_ - v25) <= band or (t25 is not None and abs(close_ - t25) <= band)
             or (gZone is not None and abs(close_ - gZone) <= band))
    onAng = onG50 or onG25
    if gDir == 1:
        gState = ("above the 50% angle - swing strong" if close_ > v50 else
                  "between 50% and 25% angles - normal pullback" if close_ > v25 else "below the 25% angle - swing failed")
    else:
        gState = ("below the 50% angle - fall strong" if close_ < v50 else
                  "between 50% and 25% angles - normal bounce" if close_ < v25 else "above the 25% angle - fall failed")
    angTxt = (f"{_fmt(deg, '#.#')}°: 50% angle {_px(v50)}, 25% angle {_px(v25)} - {gState}"
              + ("  ← ON AN ANGLE LEVEL" if onAng else ""))

    # ── sideways: each of the last 3 swings smaller than 2 x the zigzag reversal
    sideways = False
    if p["side_on"] and nZ >= 4:
        sideways = True
        for k in range(nZ - 3, nZ):
            if abs(zP[k] - zP[k - 1]) >= 2.0 * p["zz_mult"] * atrV:
                sideways = False
    conf = int(onQ1) + int(onA) + int(onQ3) + int(onAng)
    sq = timeSq and (onQ1 or onA or onQ3 or onAng) and not extending and not sideways

    # ── history test on this symbol's own swings ───────────────────────────
    (txT, okT, zT), (txR, okR, zR), (txD, okD, zD) = _history_test(zI, zP)
    works = int(okT) + int(okR) + int(okD)

    # ── texts (same wording as the Pine table and alerts) ──────────────────
    lvTxt = (("the 1x1 angle " + _px(a1)) if onA else ("target " + _px(q3Near)) if onQ3 else
             ("the 50% swing angle" if onG50 else "the 25% swing angle") if onAng else q1Txt)
    lvTxt += f" (+{conf - 1} more tools agree)" if conf >= 2 else ""
    turnTxt = (f"current move is UP from {_px(p0)} -> expect a TOP" if dir_ == 1
               else f"current move is DOWN from {_px(p0)} -> expect a BOTTOM")
    tf = timeframe
    alert_sq = (f"{ticker} {tf}: PRICE-TIME SQUARE NOW at {_px(close_)} - {nxtN} time square due, price on {lvTxt}. "
                f"{turnTxt}. " + ("Price is AHEAD of time (strong) - wait for a clear reversal candle." if ahead
                                  else "Enter only on a reversal candle; stop beyond its extreme."))
    alert_watch = (f"{ticker} {tf}: time square ({nxtN}) due now, price {_px(close_)} not on a level yet - "
                   f"{turnTxt}. Watch nearest level {q1Txt}.")

    def d_(i):
        try:
            return str(D[i])[:10]
        except IndexError:
            return ""

    if sq:
        sq_txt = ("PRICE-TIME SQUARE NOW" + (f" ★{conf} tools agree" if conf >= 2 else "")
                  + " - time square due and price on a level: expect a turn; enter only on a reversal candle")
        state = "square"
    elif timeSq and sideways:
        sq_txt, state = "Time square due, but the market is SIDEWAYS (last swings small) - skip", "sideways_due"
    elif timeSq:
        sq_txt, state = "Time square due, but price is not on a level - watch", "due"
    elif sideways:
        sq_txt, state = "No square now - market sideways", "sideways"
    else:
        sq_txt, state = "No square now", "none"
    if dMin == 0:
        when = "NOW"
    elif dMin > 0:
        when = f"in {dMin} bars"
    else:
        when = f"{-dMin} bars ago"
    fut = "-" if nxtB is None else (f"{nxtN} {when} (±{tolT})  | range/S date: {tR - b} bars from now")

    out.update(
        ok=True, square=bool(sq), state=state, time_square=bool(timeSq), extending=bool(extending),
        sideways=bool(sideways), provisional=provisional, conf=conf,
        on_level=bool(onQ1), on_1x1=bool(onA), on_target=bool(onQ3), on_angle=bool(onAng),
        ahead=bool(ahead), expect="TOP" if dir_ == 1 else "BOTTOM", move_dir="up" if dir_ == 1 else "down",
        move_start=round(p0, 2), move_start_date=d_(i0), move_bars=el, move_range=round(r, 2),
        move_R=round(r / max(R, 1e-10), 2),
        dna_dir="UP" if dnaUp else "DOWN", dna_start=d_(iA), dna_end=d_(iB),
        dna_from=round(pA, 2), dna_to=round(pB, 2), R=round(R, 2), T=int(T), S=round(S, 4),
        next_square=nxtN or None, bars_to_square=None if nxtB is None else int(dMin), tol_bars=int(tolT),
        range_s_bars=int(tR - b), level_1x1=round(a1, 2), nearest_level=None if q1Near is None else round(q1Near, 2),
        nearest_level_txt=q1Txt, nearest_target=None if q3Near is None else round(q3Near, 2), targets=targets,
        angle_deg=round(deg, 1), angle50=round(v50, 2), angle25=round(v25, 2), angle_state=gState,
        band=round(band, 2), atr=round(float(atrV), 2),
        rules_work=works, test_time=txT, test_range=txR, test_duration=txD,
        z_time=zT, z_range=zR, z_duration=zD,
        square_key=None if nxtB is None else f"{ticker}|{d_(i0)}|{nxtB - i0}",
        alert=alert_sq if sq else None, watch_alert=alert_watch if (timeSq and not sq) else None,
        table={
            "DNA swing": (("(provisional) " if provisional else "") + ("UP " if dnaUp else "DOWN ")
                          + f"{_d(D, iA)} → {_d(D, iB)}:  R {_px(R)} in T {T} bars  →  S {_fmt(S, '#.###')} per bar"),
            "Current move": (("up" if dir_ == 1 else "down") + f" from {_px(p0)}, {el} bars, range so far {_px(r)} "
                             f"({_fmt(r / max(R, 1e-10), '#.##')}R)"),
            "1. Price vs price": "nearest " + q1Txt + ("  ← ON IT" if onQ1 else ""),
            "2. Price vs time": f"1x1 {_px(a1)}: {q2Txt}",
            "3. Range vs time": q3Txt + ("  ← ON A TARGET" if onQ3 else ""),
            "4. Future time": fut,
            "5. Swing angles": angTxt,
            "Square": sq_txt,
            "Test: time = range/S": txT,
            "Test: range = k x R": txR,
            "Test: time = k x T": txD + "   (k = 0.5, 1, 1.5, 2 ±10%)",
        },
    )
    return out


def _d(D, i) -> str:
    """str.format_time(time, 'dd MMM yy')."""
    try:
        import pandas as pd
        return pd.Timestamp(D[i]).strftime("%d %b %y")
    except Exception:
        return str(D[i])[:10]
