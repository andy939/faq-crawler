#!/usr/bin/env python3
"""
比較「直接抓」與「維持連線／帶 cookie 驗證」等幾種抓法的速度與流量。
取資料庫裡最新的 N 筆（清單本來就是新到舊排序）。

    python3 bench.py            # 100 筆
    python3 bench.py --n 30     # 少一點，測試用

同時統計：一筆在網頁上的資料總量 vs 我們真正要的 5 個欄位有多大。
"""

import argparse
import io
import json
import re
import sqlite3
import ssl
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

# Windows 的主控台預設是 cp950，印繁體中文會 UnicodeEncodeError。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


import faqlib as F

DB = F.DB
UA = F.UA
LIST_URL = F.LIST_URL
COUNTER_URL = F.COUNTER_URL


def new_session(keep_alive=True, gzip=True):
    return F.new_session(keep_alive, gzip, pool=4)


# ---- 欄位抽取（用 faqlib，跟正式程式同一份邏輯）--------------------------
def extract(html_text):
    d = F.parse_detail(html_text)
    return {"標題": d["title"], "發布時間": d["published"],
            "更新時間": d["updated"], "發布機關": d["dept"],
            "內容": d["body"]}


# ---- 各種抓法 ---------------------------------------------------------------
def run(name, urls, *, keep_alive=True, gzip=True, workers=1,
        warm_session=False, hit_counter=False, per_request_session=False,
        per_thread_session=False):
    """回傳 (耗時秒, 線上位元組, 解壓後位元組, 請求數, 抽出的資料)"""
    wire = [0]
    raw = [0]
    reqs = [0]
    rows = []
    shared = None
    if not (per_request_session or per_thread_session):
        shared = new_session(keep_alive, gzip)
        if warm_session:
            r = shared.get(LIST_URL, timeout=30)   # 先進清單頁拿 cookie
            wire[0] += int(r.headers.get("Content-Length") or len(r.content))
            raw[0] += len(r.content)
            reqs[0] += 1

    tl = threading.local()

    def one(u):
        if per_request_session:
            s = new_session(keep_alive, gzip)
        elif per_thread_session:
            s = getattr(tl, "s", None)
            if s is None:
                s = tl.s = new_session(keep_alive, gzip)
        else:
            s = shared
        headers = {"Referer": LIST_URL} if warm_session else {}
        r = s.get(u, headers=headers, timeout=30)
        r.encoding = "utf-8"
        wire[0] += int(r.headers.get("Content-Length") or len(r.content))
        raw[0] += len(r.content)
        reqs[0] += 1
        if hit_counter:   # 真瀏覽器會額外打這支拿點閱數
            sid = re.search(r"[?&]s=([0-9A-F]+)", u).group(1)
            c = s.post(COUNTER_URL,
                       data={"n": "EEC70A4186D4C828", "s": sid,
                             "smlsn": "87415A8B9CE81B16"},
                       headers={"Referer": u}, timeout=30)
            wire[0] += int(c.headers.get("Content-Length") or len(c.content))
            raw[0] += len(c.content)
            reqs[0] += 1
        if per_request_session:
            s.close()
        return extract(r.text)

    t0 = time.perf_counter()
    if workers == 1:
        rows = [one(u) for u in urls]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(one, urls))
    el = time.perf_counter() - t0
    if shared:
        shared.close()
    return name, el, wire[0], raw[0], reqs[0], rows


def human(b):
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024 or u == "GB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:,.1f} {u}"
        b /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    urls = [r[0] for r in con.execute(
        "SELECT url FROM faq WHERE kind='internal' ORDER BY no LIMIT ?",
        (a.n,))]
    if len(urls) < a.n:
        sys.exit(f"資料庫只有 {len(urls)} 筆，先跑 crawl_faq.py list")

    # rounds=每個方法測幾輪；慢而且結論很明確的只測一輪
    plans = [
        ("A  每筆重開連線（沒用 Session 的寫法）", 1,
         dict(keep_alive=False, per_request_session=True)),
        ("B  直接抓：Session 續用連線・循序", a.rounds,
         dict()),
        ("C  同 B 但關掉 gzip", 1,
         dict(gzip=False)),
        ("D  完整流程：cookie + Referer + 點閱數回報・循序", a.rounds,
         dict(warm_session=True, hit_counter=True)),
        ("E  直接抓・併發 3", a.rounds,
         dict(workers=3, per_thread_session=True)),
        ("F  直接抓・併發 8", a.rounds,
         dict(workers=8, per_thread_session=True)),
    ]

    print(f"測試對象：最新 {len(urls)} 筆常見問答")
    print(f"每個方法交錯測 {a.rounds} 輪取中位數（伺服器延遲會飄，單次量不準）\n")

    times = {name: [] for name, _, _ in plans}
    meta = {}
    maxr = max(r for _, r, _ in plans)
    for rd in range(1, maxr + 1):
        print(f"--- 第 {rd} 輪 ---")
        for name, rounds, kw in plans:
            if rd > rounds:
                continue
            res = run(name, urls, **kw)
            times[name].append(res[1])
            meta[name] = res
            print(f"  {name:<44}{res[1]:>7.1f}s")
            time.sleep(3)

    print("\n" + "=" * 82)
    print(f"{'方法':<46}{'中位':>8}{'每筆':>9}{'req/s':>8}{'流量':>11}")
    print("-" * 82)
    order = []
    for name, _, _ in plans:
        ts = times[name]
        med = statistics.median(ts)
        _, _, wire, raw, reqs, rows = meta[name]
        order.append((name, med, wire))
        rng = f"  (n={len(ts)}: " + " / ".join(f"{t:.1f}" for t in ts) + ")"
        print(f"{name:<46}{med:>7.1f}s{med/len(urls)*1000:>8.0f}ms"
              f"{reqs/med:>8.1f}{human(wire):>12}")
        if len(ts) > 1:
            print(f"{'':<46}{rng}")
    print("=" * 82)

    base = statistics.median(times[plans[1][0]])
    print(f"\n以 B（直接抓・循序）為基準：")
    for name, med, _ in order:
        bar = "█" * max(1, round(med / base * 8))
        print(f"  {name:<46}{med/base:>6.2f}x  {bar}")

    # ---- 資料量分析 ------------------------------------------------------
    _, _, wire, raw, _, rows = meta[plans[1][0]]
    n = len(urls)
    fields = json.dumps(rows, ensure_ascii=False)
    body_len = statistics.mean(len(r["內容"]) for r in rows)
    kept = len(fields.encode()) / n
    print("\n" + "=" * 82)
    print("一筆資料在網頁上的量")
    print("-" * 82)
    print(f"  伺服器產出的完整 HTML     {human(raw/n):>12}")
    print(f"  實際下載（gzip 後）       {human(wire/n):>12}   "
          f"= 原始的 {wire/raw*100:.0f}%")
    print(f"  你要的 5 個欄位            {human(kept):>12}   "
          f"= 原始的 {kept/(raw/n)*100:.1f}%")
    print(f"  其中「內容」平均           {body_len:>9.0f} 字")
    print(f"\n  → 全站 8,277 筆：要下載 {human(wire/n*8277)}，"
          f"存下來只有 {human(kept*8277)}")
    print("=" * 82)

    print("\n抽出來長這樣（第 1 筆）：")
    r = rows[0]
    for k in ["標題", "發布機關", "發布時間", "更新時間"]:
        print(f"  {k}：{r[k]}")
    print(f"  內容：{r['內容'][:100]}…")
    miss = {k: sum(1 for r in rows if not r[k])
            for k in ["標題", "發布時間", "更新時間", "發布機關", "內容"]}
    print(f"\n{len(rows)} 筆裡抽不到的欄位：{miss}")


if __name__ == "__main__":
    main()
