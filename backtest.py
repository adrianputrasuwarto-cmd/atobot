#!/usr/bin/env python3
"""
backtest.py - Uji historis SARAN bot (bukan auto trading) untuk XAUUSD.

Cara kerja (walk-forward, tanpa mengintip masa depan):
  1. Untuk setiap jam di masa lalu, pakai HANYA candle yang sudah tertutup saat itu,
     jalankan logika zona yang sama dengan atobot_cloud.py, hasilkan rencana BUY dan SELL.
  2. Simulasikan rencana itu pada candle M15 berikutnya:
     - order limit di area entry (layer pertama), berlaku ENTRY_BARS candle
     - setelah terisi: TP kena dulu = menang, SL kena dulu = kalah
       (bila TP dan SL di candle yang sama, dianggap SL dulu = konservatif)
     - tidak kena apa pun dalam HOLD_BARS candle = ditutup di harga saat itu
  3. Hasil dihitung dalam satuan R (1R = jarak entry ke SL), setelah biaya spread.
  4. Hasil dipecah per jenis saran supaya terlihat mana yang bagus dan mana yang jelek.

Butuh env: TWELVE_API_KEY. Opsional: TELEGRAM_TOKEN + TELEGRAM_CHAT_ID (kirim ringkasan).
  MOCK=1 untuk uji logika dengan data tiruan.
"""
import bisect
import csv
import datetime as dt
import os
import sys
import time

import atobot_cloud as bot

MOCK = os.environ.get("MOCK", "").strip() == "1"
STEP = int(bot.fenv("STEP", 4))                # evaluasi tiap N candle M15 (4 = tiap jam)
M15_TOTAL = int(bot.fenv("M15_TOTAL", 10000))   # jumlah candle M15 yang diambil (~4 bulan)
ENTRY_BARS = int(bot.fenv("ENTRY_BARS", 16))    # order limit berlaku 16 x M15 = 4 jam
HOLD_BARS = int(bot.fenv("HOLD_BARS", 96))      # tahan maksimal 96 x M15 = 24 jam
COST = bot.fenv("COST", 0.35)                   # biaya spread per trade (dolar per oz)

TF = [("M15", "15min", 15, 220), ("M30", "30min", 30, 220), ("H1", "1h", 60, 300)]
LOOKBACK = 120


# ------------------------------------------------------------------ data
def fetch_all(interval, total):
    """Ambil candle (urut lama -> baru) dengan paging mundur lewat end_date."""
    out, end = [], None
    while len(out) < total:
        params = dict(symbol=bot.SYMBOL, interval=interval, outputsize=min(5000, total - len(out)),
                      timezone="UTC", order="DESC")
        if end:
            params["end_date"] = end
        d = bot.td_get("time_series", **params)
        rows = d.get("values") or []
        if not rows:
            break
        page = [{"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
                 "l": float(v["low"]), "c": float(v["close"])} for v in rows]
        out = out + page
        oldest = page[-1]["t"]
        end = (dt.datetime.strptime(oldest[:19], "%Y-%m-%d %H:%M:%S" if " " in oldest else "%Y-%m-%d")
               - dt.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        if len(page) < 100:
            break
        time.sleep(8)          # batas gratis 8 permintaan per menit
    seen, uniq = set(), []
    for b in out:
        if b["t"] not in seen:
            seen.add(b["t"])
            uniq.append(b)
    uniq.reverse()
    return uniq


def parse(t):
    return dt.datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S" if " " in t else "%Y-%m-%d")


def synthetic():
    data = {}
    for name, _, mins, _ in TF:
        n = {15: 6000, 30: 3000, 60: 1500}[mins]
        step = {15: 3.0, 30: 4.0, 60: 5.0}[mins]
        raw = list(reversed(bot.mock_bars(n, step, mins)))
        t0 = dt.datetime(2026, 1, 5)
        for i, b in enumerate(raw):
            b["t"] = (t0 + dt.timedelta(minutes=mins * i)).strftime("%Y-%m-%d %H:%M:%S")
        data[name] = raw
    d1 = list(reversed(bot.mock_bars(200, 30.0, 3)))
    for i, b in enumerate(d1):
        b["t"] = (dt.datetime(2026, 1, 1) + dt.timedelta(days=i)).strftime("%Y-%m-%d")
    data["D1"] = d1
    return data


# ------------------------------------------------------------------ simulasi
def ema(vals, p):
    k, e = 2.0 / (p + 1), vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def simulate(pl, m15, i0, tp_r=None):
    """Satu rencana: kembalikan (status, R). i0 = indeks candle M15 terakhir yang sudah tertutup."""
    buy, entry, sl, tp = pl["buy"], pl["entries"][0], pl["sl"], pl["tp"]
    risk = abs(entry - sl)
    if tp_r is not None:
        tp = entry + tp_r * risk if buy else entry - tp_r * risk
    if risk <= 0:
        return "skip", 0.0
    filled_at = None
    for j in range(i0 + 1, min(len(m15), i0 + 1 + ENTRY_BARS)):
        b = m15[j]
        if (b["l"] <= entry) if buy else (b["h"] >= entry):
            filled_at = j
            break
    if filled_at is None:
        return "tidak terisi", 0.0
    for j in range(filled_at, min(len(m15), filled_at + HOLD_BARS)):
        b = m15[j]
        hit_sl = (b["l"] <= sl) if buy else (b["h"] >= sl)
        hit_tp = (b["h"] >= tp) if buy else (b["l"] <= tp)
        if hit_sl:                       # SL didahulukan bila satu candle kena keduanya
            return "kalah", (-1.0 - COST / risk)
        if hit_tp:
            return "menang", (abs(tp - entry) / risk - COST / risk)
    j = min(len(m15) - 1, filled_at + HOLD_BARS - 1)
    pnl = (m15[j]["c"] - entry) if buy else (entry - m15[j]["c"])
    return "waktu habis", (pnl - COST) / risk


# ------------------------------------------------------------------ jalankan
def main():
    if not MOCK and not bot.TD_KEY:
        print("Isi secret TWELVE_API_KEY dulu.")
        return 1
    if MOCK:
        data = synthetic()
    else:
        data = {}
        for name, interval, mins, _ in TF:
            total = M15_TOTAL if mins == 15 else (M15_TOTAL // (mins // 15)) + 400
            print(f"Ambil {name}...", flush=True)
            data[name] = fetch_all(interval, total + (0 if mins == 15 else 0))
            time.sleep(8)
        print("Ambil D1...", flush=True)
        data["D1"] = fetch_all("1day", 600)

    opens = {name: [parse(b["t"]) for b in data[name]] for name, _, _, _ in TF}
    d1_dates = [parse(b["t"]).date() for b in data["D1"]]
    m15 = data["M15"]
    print({k: len(v) for k, v in data.items()}, "periode",
          m15[0]["t"], "->", m15[-1]["t"], flush=True)

    rows, seen = [], set()
    start_i = 260
    for i in range(start_i, len(m15) - ENTRY_BARS - 2, STEP):
        T = opens["M15"][i] + dt.timedelta(minutes=15)        # waktu penutupan candle i
        if T.weekday() == 5 or (T.weekday() == 4 and T.hour >= 22) or (T.weekday() == 6 and T.hour < 22):
            continue
        view = {}
        okay = True
        for name, _, mins, size in TF:
            k = bisect.bisect_right(opens[name], T - dt.timedelta(minutes=mins))   # candle tertutup
            if k < 60:
                okay = False
                break
            view[name] = list(reversed(data[name][max(0, k - size):k]))
        if not okay:
            continue
        kd = bisect.bisect_left(d1_dates, T.date())
        if kd < 5:
            continue
        d1 = list(reversed(data["D1"][max(0, kd - 20):kd]))
        price = view["M15"][0]["c"]
        atr_ref = bot.atr14(view["M15"])
        gap = bot.MIN_GAP_ATR * atr_ref
        stats = {"S_broken": set(), "D_broken": set()}
        lists = [bot.find_zones(view[n], n, lb, stats) for n, _, _, lb in
                 [(a, b, c, LOOKBACK) for a, b, c, _ in TF]]
        zones = bot.merge_all(lists)
        highs, lows = [], []
        for n, _, _, _ in TF:
            h, l = bot.swing_levels(view[n], 200)
            highs += h
            lows += l
        zb = bot.pick_zone(zones, True, price, gap) or bot.fallback_zone(True, price, gap, atr_ref, highs, lows, "M15")
        zs = bot.pick_zone(zones, False, price, gap) or bot.fallback_zone(False, price, gap, atr_ref, highs, lows, "M15")
        pv = bot.pivots_d1(d1)
        closes_h1 = [b["c"] for b in reversed(view["H1"][:80])]
        trend_up = ema(closes_h1, 20) > ema(closes_h1, 50)
        for buy, z in ((True, zb), (False, zs)):
            if not z:
                continue
            pl = bot.build_plan(buy, z, zones, price, pv, 1.10)
            key = (buy, round(pl["start"], 1), round(pl["sl"], 1))
            if key in seen:
                continue
            seen.add(key)
            status, r = simulate(pl, m15, i)
            _, r2 = simulate(pl, m15, i, 2.0)
            rows.append({
                "waktu": T.strftime("%Y-%m-%d %H:%M"), "sisi": "BUY" if buy else "SELL",
                "tipe": "swing" if pl["weak"] else z["pattern"], "tf": z["tf"],
                "tested": int(z["tested"]), "both": int(z["both"]),
                "searah_h1": int(buy == trend_up), "dekat": int(bot.is_near(z, buy, price)),
                "jarak_atr": round(pl["dist_atr"], 2), "rr": round(abs(pl["tp"] - pl["entries"][0]) /
                                                                 max(1e-9, abs(pl["entries"][0] - pl["sl"])), 2),
                "hasil": status, "R": round(r, 3), "R2": round(r2, 3),
            })
    print("rencana unik:", len(rows), flush=True)
    return summarize(rows)


# ------------------------------------------------------------------ laporan
def stat(rs, key="R"):
    n = len(rs)
    filled = [r for r in rs if r["hasil"] not in ("tidak terisi", "skip")]
    f = len(filled)
    if f == 0:
        return n, 0, 0, 0.0, 0.0
    win = sum(1 for r in filled if r[key] > 0)
    exp = sum(r[key] for r in filled) / f
    return n, f, win, 100.0 * win / f, exp


def line(label, rs, key="R"):
    n, f, w, wr, ex = stat(rs, key)
    if f < 5:
        return f"{label:<26} rencana {n:>4} | terisi {f:>3} (data terlalu sedikit)"
    return f"{label:<26} rencana {n:>4} | terisi {f:>3} | menang {wr:>4.0f}% | rata2 {ex:+.2f}R"


def summarize(rows):
    if not rows:
        print("Tidak ada rencana.")
        return 1
    with open("backtest_hasil.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    out = []
    out.append(f"UJI HISTORIS BOT SARAN XAUUSD ({bot.MODE.upper()}, tiap {STEP * 15} menit)")
    out.append(f"Periode {rows[0]['waktu']} s/d {rows[-1]['waktu']}")
    out.append("R = 1 kali jarak entry ke SL. Biaya spread sudah dihitung. TP dan SL sama candle = SL.")
    out.append("")
    out.append(line("SEMUA rencana", rows))
    out.append(line("  BUY", [r for r in rows if r["sisi"] == "BUY"]))
    out.append(line("  SELL", [r for r in rows if r["sisi"] == "SELL"]))
    out.append("")
    out.append("Per jarak dari harga:")
    out.append(line("  dekat zona (alert)", [r for r in rows if r["dekat"]]))
    out.append(line("  <= 1.0 ATR", [r for r in rows if r["jarak_atr"] <= 1.0]))
    out.append(line("  1.0-1.5 ATR", [r for r in rows if 1.0 < r["jarak_atr"] <= 1.5]))
    out.append(line("  > 1.5 ATR (jauh)", [r for r in rows if r["jarak_atr"] > 1.5]))
    out.append("")
    out.append("Per jenis zona:")
    for t in ("RBR", "DBR", "RBD", "DBD", "swing"):
        out.append(line("  " + t, [r for r in rows if r["tipe"] == t]))
    out.append(line("  fresh (belum diuji)", [r for r in rows if not r["tested"]]))
    out.append(line("  tested", [r for r in rows if r["tested"]]))
    out.append(line("  2TF (ada di 2 TF)", [r for r in rows if r["both"]]))
    out.append("")
    out.append("Terhadap trend H1 (EMA20 vs EMA50):")
    out.append(line("  searah trend", [r for r in rows if r["searah_h1"]]))
    out.append(line("  melawan trend", [r for r in rows if not r["searah_h1"]]))
    out.append("")
    out.append("Kandidat filter v3:")
    out.append(line("  searah + <=1.0 ATR", [r for r in rows if r["searah_h1"] and r["jarak_atr"] <= 1.0]))
    out.append(line("  searah + zona base", [r for r in rows if r["searah_h1"] and r["tipe"] != "swing"]))
    out.append(line("  searah+base+<=1.0ATR", [r for r in rows if r["searah_h1"] and r["tipe"] != "swing"
                                               and r["jarak_atr"] <= 1.0]))
    out.append(line("  searah+base+fresh", [r for r in rows if r["searah_h1"] and r["tipe"] != "swing"
                                            and not r["tested"]]))
    rrs = sorted(r["rr"] for r in rows)
    med_rr = rrs[len(rrs) // 2]
    out.append("")
    out.append("Bila TP selalu dipasang 2R (2x jarak risiko) sebagai pembanding:")
    out.append(line("  semua rencana", rows, "R2"))
    out.append(line("  searah+base+<=1.0ATR", [r for r in rows if r["searah_h1"] and r["tipe"] != "swing"
                                               and r["jarak_atr"] <= 1.0], "R2"))
    out.append(line("  dekat zona (alert)", [r for r in rows if r["dekat"]], "R2"))
    out.append("")
    out.append(f"Risk reward TP bot: median {med_rr:.1f} (TP jauh = jarang tercapai dalam 24 jam).")
    out.append("Impas bila menang: 2R -> 34%+, 1R -> 50%+ (sebelum biaya).")
    out.append("Ini uji masa lalu pada data per candle, bukan jaminan hasil ke depan.")
    text = "\n".join(out)
    print(text)
    with open("backtest_ringkasan.txt", "w", encoding="utf-8") as fh:
        fh.write(text)
    if not MOCK and bot.TG_TOKEN and bot.TG_CHAT:
        try:
            bot.tg_send(text)
        except Exception as e:
            print("Telegram gagal:", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("GAGAL:", str(e)[:400])
        sys.exit(1)
