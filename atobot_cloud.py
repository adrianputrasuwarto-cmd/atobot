#!/usr/bin/env python3
"""
atobot_cloud.py - Saran XAUUSD ke Telegram (tanpa komputer, jalan di GitHub Actions).

Hanya memberi SARAN. Tidak membuka order. Anda eksekusi manual di HP.
Logika zona sama dengan AtoBotTrading (mode PAC): base RBR/DBR/RBD/DBD,
zona H1 + H4 (2TF), pivot D1, lot dari risiko % ekuitas.

Rahasia dibaca dari environment (GitHub Secrets):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVE_API_KEY
Pengaturan opsional (GitHub Variables / env):
  EQUITY=990  CCY=EUR  RISK=1.0  LAYERS=2  NEAR_ATR=0.3  SUMMARY=1
  MODE=scalp (zona M15+M30, peta H1) atau swing (zona H1+H4)
  FAR_ATR=1.5  ALERT_EVERY=30 (menit, jeda alert dekat zona)
  FORCE=1  -> kirim ringkasan sekarang (dipakai tombol Run manual)
  PAUSE=1  -> bot dijeda (tidak kirim apa pun kecuali Run manual). Atur di Settings > Variables
  SESSION_ONLY=1 -> alert dekat zona hanya saat sesi London/New York
  USE_US10Y=1 (+ US10Y_SYMBOL) -> tampilkan arah yield AS 10 tahun (eksperimen, +1 permintaan data)
  MOCK=1   -> data tiruan untuk uji tanpa internet (tidak kirim Telegram)
"""
import datetime as dt
import math
import os
import random
import sys
import time

import requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TD_KEY = os.environ.get("TWELVE_API_KEY", "").strip()


def fenv(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


EQUITY = fenv("EQUITY", 990.0)
CCY = (os.environ.get("CCY", "").strip() or "EUR").upper()
RISK = fenv("RISK", 1.0)
LAYERS = int(fenv("LAYERS", 2))
NEAR_ATR = fenv("NEAR_ATR", 0.3)
SUMMARY = os.environ.get("SUMMARY", "1").strip() != "0"
FORCE = os.environ.get("FORCE", "").strip() == "1"
MOCK = os.environ.get("MOCK", "").strip() == "1"
SYMBOL = os.environ.get("SYMBOL", "XAU/USD").strip()
CONTRACT = fenv("CONTRACT", 100.0)      # oz per 1 lot
MODE = (os.environ.get("MODE", "").strip().lower() or "scalp")
FAR_ATR = fenv("FAR_ATR", 1.5)           # zona lebih jauh dari ini (x ATR) ditandai "tunggu"
ALERT_EVERY = int(fenv("ALERT_EVERY", 10))   # 10 = alert di setiap putaran selama dekat zona
FX_FALLBACK = fenv("FX_FALLBACK", 1.10)  # EUR/USD cadangan bila gagal diambil


def tf_config():
    """(nama, interval Twelve Data, jumlah candle diminta, lookback zona)"""
    if MODE == "swing":
        return [("H1", "1h", 300, 120), ("H4", "4h", 260, 200)]
    return [("M15", "15min", 220, 120), ("M30", "30min", 220, 120), ("H1", "1h", 300, 120)]

# Parameter zona (sama dengan cBot)
BASE_BODY_ATR = 0.5
BASE_MAX = 3
LEG_IN_ATR = 1.0
IMPULSE_ATR = 1.5
IMPULSE_BARS = 6
MAX_ZONE_ATR = 2.5
SWING_K = 3
MIN_GAP_ATR = 0.10
CL_BUF_ATR = 0.10
SL_BUF_ATR = 0.10
MIN_RR = 1.0
FIB_RATIOS = [0.382, 0.5, 0.618, 0.786]
FIB_NAMES = ["38.2", "50.0", "61.8", "78.6"]


# ------------------------------------------------------------------ data
def td_get(path, **params):
    params["apikey"] = TD_KEY
    r = requests.get("https://api.twelvedata.com/" + path, params=params, timeout=20)
    d = r.json()
    if isinstance(d, dict) and d.get("status") == "error":
        raise RuntimeError("Twelve Data: " + str(d.get("message", "error")).replace(TD_KEY, "***")[:200])
    return d


def get_bars(interval, size):
    """Candle TERTUTUP (terbaru dulu) dan harga terakhir (close candle yang masih berjalan)."""
    d = td_get("time_series", symbol=SYMBOL, interval=interval, outputsize=size)
    rows = d.get("values") or []
    bars = [{"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
             "l": float(v["low"]), "c": float(v["close"])} for v in rows]
    if len(bars) < 2:
        raise RuntimeError("Data " + interval + " kosong.")
    return bars[1:], bars[0]["c"]


def usd_per_ccy():
    if CCY == "USD":
        return 1.0
    try:
        return float(td_get("price", symbol=CCY + "/USD")["price"])
    except Exception:
        return FX_FALLBACK


def mock_bars(n, step, seed):
    """Data tiruan: tren naik-turun dengan beberapa gerak impuls, untuk uji logika."""
    rnd = random.Random(seed)
    price = 4000.0
    out = []
    drift = 0.0
    for i in range(n):
        if i % 23 == 0:
            drift = rnd.choice([-1, 1]) * step * 0.9
        o = price
        c = o + drift + rnd.uniform(-step * 0.5, step * 0.5)
        if rnd.random() < 0.15:
            c = o + rnd.choice([-1, 1]) * step * rnd.uniform(1.2, 2.5)
        h = max(o, c) + rnd.uniform(0, step * 0.4)
        l = min(o, c) - rnd.uniform(0, step * 0.4)
        out.append({"t": str(i), "o": o, "h": h, "l": l, "c": c})
        price = c
    return list(reversed(out))   # terbaru dulu


# ------------------------------------------------------------------ hitung
def atr14(b):
    """ATR sederhana 14 pada candle tertutup terbaru. b terbaru dulu."""
    trs = []
    for i in range(14):
        h, l, pc = b[i]["h"], b[i]["l"], b[i + 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / 14.0


def has_fvg(L, start_idx, lowest_idx, supply):
    for j in range(start_idx, lowest_idx - 1, -1):
        a, c = j + 1, j - 1
        if supply:
            if L(a)["l"] > L(c)["h"]:
                return True
        else:
            if L(a)["h"] < L(c)["l"]:
                return True
    return False


def find_zones(b, tf_name, lookback, stats):
    """Port ComputePacZonesFor. Indeks Last(j) -> b[j-1] (Last(1) = candle tertutup terbaru)."""
    def L(j):
        return b[j - 1]

    if len(b) < 20:
        return []
    atr = atr14(b)
    if atr <= 0:
        return []
    W = IMPULSE_BARS
    n = min(len(b) - 1, lookback)
    if n < W + 8:
        return []
    min_h = 0.15 * atr
    zones = []
    sup = dem = 0

    for b_new in range(2, n - W):
        for ln in range(1, BASE_MAX + 1):
            b_old = b_new + ln - 1
            if b_old + W > n:
                break
            b_high = -1e18
            b_low = 1e18
            top_body = -1e18
            bot_body = 1e18
            ok = True
            for j in range(b_new, b_old + 1):
                o, c = L(j)["o"], L(j)["c"]
                if abs(c - o) > BASE_BODY_ATR * atr:
                    ok = False
                    break
                b_high = max(b_high, L(j)["h"])
                b_low = min(b_low, L(j)["l"])
                top_body = max(top_body, o, c)
                bot_body = min(bot_body, o, c)
            if not ok:
                continue

            out_end = max(1, b_new - W)
            max_out = -1e18
            min_out = 1e18
            close_above = close_below = False
            for j in range(b_new - 1, out_end - 1, -1):
                max_out = max(max_out, L(j)["h"])
                min_out = min(min_out, L(j)["l"])
                cc = L(j)["c"]
                if cc > b_high:
                    close_above = True
                if cc < b_low:
                    close_below = True
            up_move = max_out - top_body
            dn_move = bot_body - min_out
            out_up = close_above and up_move >= IMPULSE_ATR * atr
            out_dn = close_below and dn_move >= IMPULSE_ATR * atr
            if not out_up and not out_dn:
                continue
            if out_up and out_dn:
                if up_move >= dn_move:
                    out_dn = False
                else:
                    out_up = False

            in_end = min(n + 1, b_old + W)
            max_in = -1e18
            min_in = 1e18
            i_max = i_min = b_old + 1
            for j in range(b_old + 1, in_end + 1):
                if L(j)["h"] > max_in:
                    max_in, i_max = L(j)["h"], j
                if L(j)["l"] < min_in:
                    min_in, i_min = L(j)["l"], j
            in_up = (b_low - min_in) >= LEG_IN_ATR * atr
            in_dn = (max_in - b_high) >= LEG_IN_ATR * atr
            if in_up and in_dn:
                if i_min < i_max:
                    in_dn = False
                else:
                    in_up = False
            if not in_up and not in_dn:
                continue

            supply = out_dn
            pattern = ("DBD" if in_dn else "RBD") if supply else ("RBR" if in_up else "DBR")

            if supply:
                top = b_high
                bottom = min(bot_body, top - min_h)
            else:
                bottom = b_low
                top = max(top_body, bottom + min_h)
            if top - bottom > MAX_ZONE_ATR * atr:
                continue

            broken = False
            for j in range(b_new - 1, 0, -1):
                cc = L(j)["c"]
                if (cc > top) if supply else (cc < bottom):
                    broken = True
                    break
            if broken:
                stats["S_broken" if supply else "D_broken"].add((round(top, 1), round(bottom, 1)))
                continue

            tested = False
            for j in range(out_end - 1, 0, -1):
                if (L(j)["h"] >= bottom) if supply else (L(j)["l"] <= top):
                    tested = True
                    break

            if (sup if supply else dem) >= 8:
                continue
            if any(z["supply"] == supply and not (z["top"] < bottom or z["bottom"] > top) for z in zones):
                continue

            z = {
                "supply": supply, "top": top, "bottom": bottom,
                "extreme": min_out if supply else max_out,
                "distal": top if supply else bottom,
                "atr": atr, "pattern": pattern, "tested": tested, "tf": tf_name,
                "fvg": has_fvg(L, b_new, max(2, b_new - W), supply), "both": False,
            }
            sweep_end = min(b_old + 10, len(b))
            new_close = L(b_new)["c"]
            if supply:
                prev = max([L(j)["h"] for j in range(b_old + 1, sweep_end + 1)] or [-1e18])
                z["sweep"] = b_high > prev and new_close < prev
            else:
                prev = min([L(j)["l"] for j in range(b_old + 1, sweep_end + 1)] or [1e18])
                z["sweep"] = b_low < prev and new_close > prev
            zones.append(z)
            if supply:
                sup += 1
            else:
                dem += 1
            break
    return zones


def merge_all(lists):
    """Tandai zona yang tumpang tindih dengan zona sejenis dari timeframe lain (+2TF)."""
    for i in range(len(lists)):
        for j in range(i + 1, len(lists)):
            for x in lists[i]:
                for y in lists[j]:
                    if x["supply"] == y["supply"] and not (x["top"] < y["bottom"] or x["bottom"] > y["top"]):
                        x["both"] = True
                        y["both"] = True
    return [z for l in lists for z in l]


def pick_zone(zones, buy, price, gap):
    best = None
    for z in zones:
        if buy:
            if z["supply"] or z["top"] >= price - gap:
                continue
            if best is None or z["top"] > best["top"] or (z["top"] == best["top"] and z["both"]):
                best = z
        else:
            if not z["supply"] or z["bottom"] <= price + gap:
                continue
            if best is None or z["bottom"] < best["bottom"] or (z["bottom"] == best["bottom"] and z["both"]):
                best = z
    return best


def swing_levels(b, lookback):
    """Swing high/low (K candle di kiri dan kanan lebih rendah/tinggi). b terbaru dulu."""
    k = SWING_K
    n = min(len(b) - 1, lookback)
    highs, lows = [], []
    for p in range(k + 1, n - k + 1):
        hi, lo = b[p - 1]["h"], b[p - 1]["l"]
        sh = all(b[p - 1 - j]["h"] < hi and b[p - 1 + j]["h"] <= hi for j in range(1, k + 1))
        sl = all(b[p - 1 - j]["l"] > lo and b[p - 1 + j]["l"] >= lo for j in range(1, k + 1))
        if sh:
            highs.append(hi)
        if sl:
            lows.append(lo)
    return highs, lows


def fallback_zone(buy, price, gap, atr_ref, highs, lows, tf_name):
    """Cadangan bila tidak ada zona base: resistance/support dari swing terdekat (kualitas lebih rendah)."""
    if buy:
        c = [x for x in lows if x < price - gap]
        if not c:
            return None
        lvl = max(c)
        top = min(lvl + 0.25 * atr_ref, price - gap)
        bottom = min(lvl, top - 0.15 * atr_ref)
    else:
        c = [x for x in highs if x > price + gap]
        if not c:
            return None
        lvl = min(c)
        bottom = max(lvl - 0.25 * atr_ref, price + gap)
        top = max(lvl, bottom + 0.15 * atr_ref)
    return {"supply": not buy, "top": top, "bottom": bottom, "extreme": lvl, "distal": lvl,
            "atr": atr_ref, "pattern": "Swing", "tested": False, "tf": tf_name,
            "fvg": False, "sweep": False, "both": False, "weak": True}


def pivots_d1(d1):
    """Pivot klasik dari candle D1 tertutup terakhir."""
    x = d1[0]
    pp = (x["h"] + x["l"] + x["c"]) / 3.0
    return pp, 2 * pp - x["l"], 2 * pp - x["h"]    # PP, R1, S1


def zone_start_end(z, buy):
    return (z["top"], z["bottom"]) if buy else (z["bottom"], z["top"])


def is_near(z, buy, price):
    start, end = zone_start_end(z, buy)
    inside = (end <= price <= start) if buy else (start <= price <= end)
    return inside or abs(price - start) <= NEAR_ATR * z["atr"]


def build_plan(buy, z, zones, price, pv, usd_pc):
    start, end = zone_start_end(z, buy)
    za = z["atr"]
    cut = end - CL_BUF_ATR * za if buy else end + CL_BUF_ATR * za
    sl = cut - SL_BUF_ATR * za if buy else cut + SL_BUF_ATR * za

    pp, r1, s1 = pv
    cand = [pp, r1, s1] + [q for zz in zones for q in (zz["top"], zz["bottom"])]
    risk_dist = abs(start - sl)
    min_tp = start + MIN_RR * risk_dist if buy else start - MIN_RR * risk_dist
    if buy:
        up = [x for x in cand if x >= min_tp]
        tp = min(up) if up else min_tp
    else:
        dn = [x for x in cand if x <= min_tp]
        tp = max(dn) if dn else min_tp

    n = max(1, LAYERS)
    entries = [start if n == 1 else start + (end - start) * i / (n - 1) for i in range(n)]

    snap = []
    if n >= 3 and not z.get("weak"):
        rng = abs(z["extreme"] - z["distal"])
        tol = 0.25 * (z["top"] - z["bottom"])
        used = []
        for i in range(1, n - 1):
            best, best_d = -1, tol
            for r in range(4):
                lvl = z["extreme"] - FIB_RATIOS[r] * rng if buy else z["extreme"] + FIB_RATIOS[r] * rng
                if lvl < z["bottom"] or lvl > z["top"] or r in used:
                    continue
                d = abs(lvl - entries[i])
                if d <= best_d:
                    best_d, best = d, r
            if best >= 0:
                used.append(best)
                entries[i] = (z["extreme"] - FIB_RATIOS[best] * rng) if buy else (z["extreme"] + FIB_RATIOS[best] * rng)
                snap.append(FIB_NAMES[best])

    loss_per_lot = sum(abs(e - sl) * CONTRACT for e in entries) / usd_pc
    risk_money = EQUITY * RISK / 100.0
    lot = math.floor((risk_money / loss_per_lot if loss_per_lot > 0 else 0) * 100 + 1e-9) / 100
    below = lot < 0.01
    if below:
        lot = 0.01
    est_loss = loss_per_lot * lot
    if z.get("weak"):
        tag = ("Support" if buy else "Resistance") + " swing (kualitas lebih rendah, bukan zona base)"
    else:
        tag = z["tf"] + " " + z["pattern"] + ("+FVG" if z["fvg"] else "") + ("+Sweep" if z["sweep"] else "") + \
            ("+2TF" if z["both"] else "") + (" (tested)" if z["tested"] else "")
    dist = abs(price - start)
    return {
        "buy": buy, "start": start, "end": end, "cut": cut, "sl": sl, "tp": tp,
        "entries": entries, "lot": lot, "est_loss": est_loss, "pct": est_loss / EQUITY * 100.0,
        "below": below, "tag": tag, "snap": snap, "dist": dist, "dist_atr": dist / za,
        "far": dist / za > FAR_ATR, "weak": bool(z.get("weak")),
    }


def p2(v):
    return f"{v:.2f}"


def fmt_plan(pl):
    side = "BUY LIMIT" if pl["buy"] else "SELL LIMIT"
    arrow = "▲" if pl["buy"] else "▼"
    lines = [
        f"{arrow} {side}  area {p2(pl['start'])} - {p2(pl['end'])}",
        f"   Zona: {pl['tag']}" + (f" | Fib {'/'.join(pl['snap'])}" if pl["snap"] else ""),
        f"   Layer: {', '.join(p2(e) for e in pl['entries'])}",
        f"   Cut Loss {p2(pl['cut'])} | SL {p2(pl['sl'])} | TP {p2(pl['tp'])}",
        f"   {len(pl['entries'])} layer x {pl['lot']:.2f} lot = {pl['lot'] * len(pl['entries']):.2f} lot"
        f" | rugi di SL ±{pl['est_loss']:.2f} {CCY} ({pl['pct']:.1f}%)",
        f"   Jarak dari harga: {pl['dist']:.1f} poin ({pl['dist_atr']:.1f} ATR)",
    ]
    if pl["far"]:
        lines.append(f"   ⏳ Terlalu jauh (> {FAR_ATR:.1f} ATR). Tunggu harga mendekat, jangan dikejar.")
    if pl["below"]:
        lines.append(f"   ⛔ LEWATI: lot minimum 0.01/layer membuat risiko {pl['pct']:.1f}%, di atas batas {RISK:.1f}%.")
    return "\n".join(lines)



# ------------------------------------------------------------------ bias (v3)
def ema_last(closes, p):
    k, e = 2.0 / (p + 1), closes[0]
    for v in closes[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_of(b):
    """Kompas trend dari candle tertutup (terbaru dulu): (teks, arah) arah +1 naik, -1 turun, 0 sideways."""
    closes = [x["c"] for x in reversed(b[:120])]
    if len(closes) < 55:
        return "n/a", 0
    e20, e50 = ema_last(closes, 20), ema_last(closes, 50)
    atr = atr14(b)
    if abs(e20 - e50) < 0.15 * atr:
        return "SIDEWAYS", 0
    if e20 > e50 and b[0]["c"] > e50:
        return "NAIK", 1
    if e20 < e50 and b[0]["c"] < e50:
        return "TURUN", -1
    return "SIDEWAYS", 0


def zscore(b, n=20):
    xs = [x["c"] for x in b[:n]]
    m = sum(xs) / len(xs)
    sd = (sum((v - m) ** 2 for v in xs) / len(xs)) ** 0.5
    return 0.0 if sd == 0 else (b[0]["c"] - m) / sd


def rsi14(b):
    cl = [x["c"] for x in reversed(b[:15])]
    g = sum(max(0, cl[i] - cl[i - 1]) for i in range(1, len(cl))) / 14.0
    l = sum(max(0, cl[i - 1] - cl[i]) for i in range(1, len(cl))) / 14.0
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def z_label(z):
    if z >= 2:
        return "sangat tinggi (rawan koreksi)"
    if z >= 1:
        return "agak tinggi"
    if z <= -2:
        return "sangat rendah (rawan pantul)"
    if z <= -1:
        return "agak rendah"
    return "normal"


def session_info(now):
    h = now.hour    # UTC
    wib = (now + dt.timedelta(hours=7)).strftime("%H:%M")
    if 0 <= h < 7:
        name, liquid = "Asia (cenderung sepi, sering sideways)", False
    elif 7 <= h < 13:
        name, liquid = "London (mulai ramai)", True
    elif 13 <= h < 17:
        name, liquid = "London + New York (paling ramai, volatil)", True
    elif 17 <= h < 21:
        name, liquid = "New York sore (mulai tenang)", True
    else:
        name, liquid = "Sepi / pergantian hari (spread bisa melebar)", False
    return f"Sesi {name} | {wib} WIB", liquid


def us10y_text():
    if os.environ.get("USE_US10Y", "").strip() != "1":
        return None
    sym = os.environ.get("US10Y_SYMBOL", "US10Y").strip() or "US10Y"
    try:
        d = td_get("time_series", symbol=sym, interval="1h", outputsize=30)
        v = [float(x["close"]) for x in (d.get("values") or [])]
        if len(v) < 10:
            return "US10Y: data tidak tersedia"
        chg = v[0] - v[min(len(v) - 1, 24)]
        if chg < -0.02:
            return f"US10Y {v[0]:.2f}% turun 24 jam (cenderung mendukung emas)"
        if chg > 0.02:
            return f"US10Y {v[0]:.2f}% naik 24 jam (cenderung menekan emas)"
        return f"US10Y {v[0]:.2f}% datar 24 jam"
    except Exception:
        return "US10Y: data tidak tersedia"


def compute_bias(bars, now, mock):
    h1 = bars.get("H1")
    h4 = bars.get("H4")
    if h4 is None:
        try:
            h4 = mock_bars(120, 12.0, 7) if mock else get_bars("4h", 120)[0]
        except Exception:
            h4 = None
    t1, d1_ = trend_of(h1) if h1 else ("n/a", 0)
    t4, d4_ = trend_of(h4) if h4 else ("n/a", 0)
    ref = h1 or bars[list(bars)[0]]
    z, r = zscore(ref), rsi14(ref)
    sess, liquid = session_info(now)
    arr = {"NAIK": "▲", "TURUN": "▼", "SIDEWAYS": "↔", "n/a": "?"}
    lines = [f"🧭 BIAS: H1 {t1} {arr[t1]} | H4 {t4} {arr[t4]}",
             f"Z-Score H1 {z:+.1f} ({z_label(z)}) | RSI H1 {r:.0f}",
             sess]
    u = None if mock else us10y_text()
    if u:
        lines.append(u)
    return {"text": "\n".join(lines), "h1": d1_, "h4": d4_, "liquid": liquid}


def reaction(b15, z, buy):
    r = b15[0]
    rng = r["h"] - r["l"]
    if rng <= 0:
        return False
    if buy:
        return r["l"] <= z["top"] and (min(r["o"], r["c"]) - r["l"]) / rng >= 0.5 and r["c"] > z["bottom"]
    return r["h"] >= z["bottom"] and (r["h"] - max(r["o"], r["c"])) / rng >= 0.5 and r["c"] < z["top"]


def plan_notes(pl, bias, b15, z):
    out = []
    d = bias["h1"]
    if d == 0:
        out.append("   ↔ H1 sideways: zona lebih mungkin bertahan, tetap tunggu konfirmasi.")
    elif (pl["buy"] and d > 0) or ((not pl["buy"]) and d < 0):
        out.append("   ✅ Searah trend H1.")
    else:
        out.append("   ⚠️ MELAWAN trend H1 (counter-trend): tunggu konfirmasi, ambil profit lebih cepat, lot lebih kecil.")
    if bias["h4"] != 0 and d != 0 and bias["h4"] != d:
        out.append("   ⚠️ H1 dan H4 beda arah: sinyal kurang bersih.")
    if is_near(z, pl["buy"], b15[0]["c"]):
        if reaction(b15, z, pl["buy"]):
            out.append("   🕯️ Ada wick penolakan di candle M15 terakhir (petunjuk saja, belum terbukti di uji).")
        else:
            out.append("   🕯️ Belum ada tanda reaksi di zona: tunggu konfirmasi sendiri.")
    return "\n".join(out)

FOOT = ("Saran saja, bukan perintah. Uji historis 3-10 bulan: zona ini sekitar impas, jadi pakai sebagai PETA level, "
        "tunggu konfirmasi sendiri, SL selalu terpasang. Hindari entry saat rilis data besar.")


# ------------------------------------------------------------------ kirim
def tg_send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text[:3900]}, timeout=30,
    )
    if not r.json().get("ok"):
        raise RuntimeError("Telegram menolak pesan (periksa token dan chat id).")


def market_closed(now):
    wd, hr = now.weekday(), now.hour      # UTC; Senin=0
    if wd == 5:
        return True
    if wd == 4 and hr >= 22:
        return True
    if wd == 6 and hr < 22:
        return True
    return False


def diag_text(zones, stats, price):
    sup = [z for z in zones if z["supply"]]
    dem = [z for z in zones if not z["supply"]]
    s_above = sum(1 for z in sup if z["bottom"] > price)
    d_below = sum(1 for z in dem if z["top"] < price)
    return (f"Diagnosa: supply valid {len(sup)} (di atas harga {s_above}, ditembus {len(stats['S_broken'])}) | "
            f"demand valid {len(dem)} (di bawah harga {d_below}, ditembus {len(stats['D_broken'])})")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    wib = now + dt.timedelta(hours=7)
    paused = os.environ.get("PAUSE", "").strip().lower() in ("1", "on", "true", "ya", "yes")
    if paused and not FORCE and not MOCK:
        print("Bot DIJEDA (variabel PAUSE). Tidak ada yang dikirim.")
        return 0

    if not MOCK:
        if not (TG_TOKEN and TG_CHAT and TD_KEY):
            print("Isi secret TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVE_API_KEY dulu.")
            return 1
        if market_closed(now) and not FORCE:
            print("Pasar emas tutup, dilewati.")
            return 0

    cfg = tf_config()
    bars, price = {}, None
    for i, (name, interval, size, lb) in enumerate(cfg):
        if MOCK:
            step = {"M15": 3.0, "M30": 4.0, "H1": 5.0, "H4": 12.0}[name]
            bars[name] = mock_bars(size, step, 10 + i)
            last = bars[name][0]["c"]
        else:
            bars[name], last = get_bars(interval, size)
        if price is None:
            price = last

    first = cfg[0][0]
    atr_ref = atr14(bars[first])
    gap = MIN_GAP_ATR * atr_ref
    stats = {"S_broken": set(), "D_broken": set()}
    lists = [find_zones(bars[name], name, lb, stats) for name, _, _, lb in cfg]
    zones = merge_all(lists)

    highs, lows = [], []
    for name, _, _, lb in cfg:
        h, l = swing_levels(bars[name], 200)
        highs += h
        lows += l

    zb = pick_zone(zones, True, price, gap) or fallback_zone(True, price, gap, atr_ref, highs, lows, first)
    zs = pick_zone(zones, False, price, gap) or fallback_zone(False, price, gap, atr_ref, highs, lows, first)

    near_sides = [(buy, z) for buy, z in ((True, zb), (False, zs)) if z and is_near(z, buy, price)]
    hourly = SUMMARY and (os.environ.get("SUMMARY_NOW", "").strip() == "1")
    alert_ok = FORCE or (wib.minute % ALERT_EVERY) < 10
    send_alert = bool(near_sides) and alert_ok
    if send_alert and os.environ.get("SESSION_ONLY", "").strip() == "1" and not FORCE and not session_info(now)[1]:
        send_alert = False
    send_summary = (hourly or FORCE) and not (near_sides and send_alert)

    names = "+".join(n for n, _, _, _ in cfg)
    if not (send_alert or send_summary):
        print(f"Tidak ada yang perlu dikirim. Harga {p2(price)} ({names}).")
        if not MOCK:
            return 0

    if MOCK or send_alert or send_summary:
        if MOCK:
            d1, _ = mock_bars(40, 30.0, 3), 0
            d1 = d1
            upc = 1.08 if CCY == "EUR" else 1.0
        else:
            d1, _ = get_bars("1day", 20)
            upc = usd_per_ccy()
    bias = compute_bias(bars, now, MOCK)
    b15 = bars[first]
    pv = pivots_d1(d1)
    pb = build_plan(True, zb, zones, price, pv, upc) if zb else None
    ps = build_plan(False, zs, zones, price, pv, upc) if zs else None

    head = (f"XAUUSD | {wib:%d %b %H:%M} WIB | harga {p2(price)} | ATR {first} {atr_ref:.1f}\n"
            f"Mode {MODE.upper()} ({names}) | Pivot D1: PP {p2(pv[0])} R1 {p2(pv[1])} S1 {p2(pv[2])}\n"
            + bias["text"] + ("\n⏸ Bot sedang DIJEDA, ini hasil jalan manual." if paused else ""))
    msgs = []

    if send_alert:
        txt = ["🔔 HARGA DEKAT ZONA", head, ""]
        for pl in (pb, ps):
            if pl and ((pl["buy"] and is_near(zb, True, price)) or ((not pl["buy"]) and is_near(zs, False, price))):
                txt += [fmt_plan(pl), plan_notes(pl, bias, b15, zb if pl["buy"] else zs), ""]
        txt.append(FOOT)
        msgs.append("\n".join(txt))
    if send_summary or MOCK:
        txt = ["📊 RINGKASAN", head, ""]
        txt.append((fmt_plan(pb) + "\n" + plan_notes(pb, bias, b15, zb)) if pb else "▲ BUY: tidak ada zona demand atau support swing di bawah harga.")
        txt.append("")
        txt.append((fmt_plan(ps) + "\n" + plan_notes(ps, bias, b15, zs)) if ps else "▼ SELL: tidak ada zona supply atau resistance swing di atas harga.")
        txt += ["", diag_text(zones, stats, price), FOOT]
        summary_msg = "\n".join(txt)
        if send_summary:
            msgs.append(summary_msg)

    if MOCK:
        print(summary_msg)
        print("near:", [("BUY" if b else "SELL") for b, _ in near_sides], "| alert_ok", alert_ok)
        return 0

    for m in msgs:
        tg_send(m)
    print(f"Selesai. Pesan terkirim: {len(msgs)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("GAGAL:", str(e)[:300])
        sys.exit(1)
