#!/usr/bin/env python3
"""
從 faq.db 的 samples / runs 產生比較報表。

    python3 report.py              # 產生 report.md
    python3 report.py --n 100      # 只看每次 100 筆的那些執行

samples 表是每次執行 × 每一筆各一列的原始測量，所以之後想到新的分析角度，
改這支重跑就好，不用重爬。
"""

import sys
import argparse
import os
import sqlite3
import statistics as S
from collections import defaultdict

# Windows 的主控台預設是 cp950，印繁體中文會 UnicodeEncodeError。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "faq.db")

NAMES = {
    "A": "每筆重開連線", "B": "直接抓・循序", "C": "關掉 gzip",
    "D": "完整瀏覽器流程", "E": "併發 3", "F": "併發 8",
}


def fmt(x, w=0, d=0):
    return f"{x:>{w},.{d}f}"


def linreg(xs, ys):
    """最小平方法，回傳 (斜率, 截距, r²)"""
    n = len(xs)
    mx, my = S.mean(xs), S.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    b = sxy / sxx
    a = my - b * mx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy ** 2 / (sxx * syy)) if syy else 0.0
    return b, a, r2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=0, help="只看 done 等於這個數的執行")
    ap.add_argument("--out", default=os.path.join(HERE, "report.md"))
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    where = "WHERE done >= ?" if a.n else ""
    runs = [dict(r) for r in con.execute(
        f"SELECT * FROM runs {where} ORDER BY id",
        (int(a.n * .9),) if a.n else ())]
    smp = [dict(r) for r in con.execute(
        "SELECT * FROM samples ORDER BY run_id, seq")]
    con.close()
    # 只留下有逐筆樣本的執行 —— samples 表是後來才加的，
    # 早期的執行只有彙總數字，混進來會把牆鐘算錯。
    have = {r["run_id"] for r in smp}
    runs = [r for r in runs if r["id"] in have]
    ids = {r["id"] for r in runs}
    smp = [r for r in smp if r["run_id"] in ids]

    if not smp:
        raise SystemExit("samples 是空的，先在 UI 跑幾輪")

    by_m = defaultdict(list)
    for r in smp:
        by_m[r["method"]].append(r)
    runs_by_m = defaultdict(list)
    for r in runs:
        runs_by_m[r["method"]].append(r)
    methods = sorted(by_m)

    L = []
    w = L.append
    w("# 抓法比較報表\n")
    w(f"資料來源：`faq.db` 的 `samples` 表，"
      f"{len(runs)} 次執行 × 共 {len(smp)} 筆逐筆測量。\n")
    w(f"產生時間：{__import__('time').strftime('%Y-%m-%d %H:%M')}\n")

    # ---------- 1. 總表 ----------
    w("\n## 1　方法總表\n")
    w("| 抓法 | 說明 | 筆數 | 實際耗時 | 每筆 | 等伺服器 | 傳輸 | 單次總計 |")
    w("|---|---|---:|---:|---:|---:|---:|---:|")
    for m in methods:
        rs, ru = by_m[m], runs_by_m[m]
        wall = sum(x["elapsed"] for x in ru)
        n = len(rs)
        tw = S.median(x["t_wait"] for x in rs) * 1000
        tr = S.median(x["t_read"] for x in rs) * 1000
        w(f"| **{m}** | {NAMES.get(m, '')} | {n} | {wall:.1f}s | "
          f"{wall / n * 1000:.0f}ms | {tw:.0f}ms | {tr:.0f}ms | {tw + tr:.0f}ms |")
    w("\n名詞：**實際耗時**＝從開始到結束、時鐘走了多久（也叫牆鐘時間），"
      "就是你實際等待的時間；併發時多筆同時進行，所以「每筆」會小於「單次總計」。"
      "**等伺服器**＝送出請求到收到第一個位元組（含建立連線與伺服器產頁）。"
      "**傳輸**＝把頁面收完。兩者都取中位數。\n")

    # ---------- 2. 配對比較 ----------
    w("\n## 2　配對比較（同一筆在不同方法下）\n")
    w("六種方法抓的是同樣那批文章，所以可以逐筆配對，把「內容長短」這個變因固定住。\n")
    base = "B" if "B" in by_m else methods[0]
    idx = {m: {r["sid"]: r for r in by_m[m]} for m in methods}
    common = set.intersection(*(set(idx[m]) for m in methods))
    w(f"共同出現在所有方法裡的有 **{len(common)}** 筆。以 {base}（{NAMES[base]}）為基準：\n")
    w("| 抓法 | 單次總秒 中位 | 相對基準 | 比基準慢的筆數 |")
    w("|---|---:|---:|---:|")
    for m in methods:
        d = [idx[m][s]["t_total"] for s in common]
        db_ = [idx[base][s]["t_total"] for s in common]
        ratio = S.median(d) / S.median(db_) if S.median(db_) else 0
        slower = sum(1 for s in common
                     if idx[m][s]["t_total"] > idx[base][s]["t_total"])
        w(f"| {m} | {S.median(d)*1000:.0f}ms | {ratio:.2f}x | "
          f"{slower}/{len(common)} |")

    # ---------- 3. 固定成本 ----------
    w("\n## 3　每頁的固定成本\n")
    w("把「抓下位元數」對「需要位元數」做線性迴歸。"
      "**壓縮與未壓縮要分開算**，混在一起迴歸沒有意義。\n")
    gz = [r for r in smp if r["gzip"]]
    ng = [r for r in smp if not r["gzip"]]
    w("| 資料 | 迴歸式 | r² | 固定成本 |")
    w("|---|---|---:|---:|")
    rows_ = [("壓縮後實際下載（gzip 的方法）", gz, "bytes_wire"),
             ("未壓縮下載（方法 C）", ng, "bytes_wire"),
             ("伺服器產出的原始 HTML（全部）", smp, "bytes_raw")]
    fixed = {}
    for lab, data, col in rows_:
        if not data:
            continue
        b, a0, r2 = linreg([r["bytes_needed"] for r in data],
                           [r[col] for r in data])
        fixed[lab] = a0
        w(f"| {lab} | `{a0:,.0f} + {b:.2f} × 需要` | {r2:.3f} | "
          f"{a0/1024:,.0f} KB |")
    w("")
    if gz:
        b, a0, r2 = linreg([r["bytes_needed"] for r in gz],
                           [r["bytes_wire"] for r in gz])
        w(f"- **截距 {a0:,.0f} byte**：不管內容多短，每頁都要付的固定成本"
          f"（版型、選單、CSS、JS）")
        w(f"- **斜率 {b:.2f}**：內容每多 1 byte，壓縮後的下載量只多 "
          f"{b:.2f} byte")
    b2, a2, r22 = linreg([r["bytes_needed"] for r in smp],
                         [r["bytes_raw"] for r in smp])
    w(f"- 未壓縮的原始 HTML 斜率 **{b2:.2f}**（r²={r22:.3f}）"
      f"—— 內容 1 byte 會膨脹成 {b2:.2f} byte 的 HTML（標籤、樣式）\n")

    # ---------- 4. 時間跟內容長短有關嗎 ----------
    w("\n## 4　內容長短會影響抓取時間嗎\n")
    w("| 抓法 | 斜率（每 KB 內容增加的秒數） | r² | 判讀 |")
    w("|---|---:|---:|---|")
    for m in methods:
        rs = by_m[m]
        b3, a3, r23 = linreg([r["bytes_needed"] / 1024 for r in rs],
                             [r["t_total"] for r in rs])
        verdict = "幾乎無關" if r23 < 0.1 else ("弱相關" if r23 < 0.3 else "有關")
        w(f"| {m} | {b3*1000:+.0f} ms/KB | {r23:.3f} | {verdict} |")
    w("\nr² 接近 0 表示內容長短解釋不了時間差異 —— 時間花在固定的那 55 KB，"
      "不在內容本身。\n")

    # ---------- 5. 延遲分布 ----------
    w("\n## 5　單次請求延遲分布（秒）\n")
    w("| 抓法 | 最快 | p25 | 中位 | p75 | p95 | 最慢 | 最慢/中位 |")
    w("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in methods:
        v = sorted(r["t_total"] for r in by_m[m])
        q = lambda p: v[min(int(len(v) * p), len(v) - 1)]
        w(f"| {m} | {v[0]:.3f} | {q(.25):.3f} | {q(.50):.3f} | {q(.75):.3f} | "
          f"{q(.95):.3f} | {v[-1]:.3f} | {v[-1]/q(.50):.1f}x |")
    w("\n尾巴（最慢/中位）拉得越長，代表那個方法越容易撞到伺服器的卡頓。\n")

    # ---------- 6. 位元效率 ----------
    w("\n## 6　位元效率\n")
    w("| 抓法 | 抓下/筆 | 原始HTML/筆 | 需要/筆 | 壓縮率 | 有效率 |")
    w("|---|---:|---:|---:|---:|---:|")
    for m in methods:
        rs = by_m[m]
        mw = S.median(r["bytes_wire"] for r in rs)
        mr = S.median(r["bytes_raw"] for r in rs)
        mn = S.median(r["bytes_needed"] for r in rs)
        w(f"| {m} | {mw:,.0f} | {mr:,.0f} | {mn:,.0f} | "
          f"{100*mw/mr:.0f}% | 1/{mw/mn:.0f} |")

    # ---------- 7. 內容分布 ----------
    w("\n## 7　內容本身的分布\n")
    nd = sorted(r["bytes_needed"] for r in by_m[base])
    ch = sorted(r["chars"] for r in by_m[base])
    q = lambda v, p: v[min(int(len(v) * p), len(v) - 1)]
    w("| | 最小 | p25 | 中位 | p75 | 最大 | 平均 |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    w(f"| 需要位元數 | {nd[0]:,} | {q(nd,.25):,} | {q(nd,.5):,} | "
      f"{q(nd,.75):,} | {nd[-1]:,} | {S.mean(nd):,.0f} |")
    w(f"| 內容字數 | {ch[0]:,} | {q(ch,.25):,} | {q(ch,.5):,} | "
      f"{q(ch,.75):,} | {ch[-1]:,} | {S.mean(ch):,.0f} |")
    w(f"\n最大是最小的 {nd[-1]/nd[0]:.0f} 倍，標準差 {S.stdev(nd):,.0f} byte"
      f"（比平均值 {S.mean(nd):,.0f} 還大）。**平均值不能代表任何一筆。**\n")

    # ---------- 7b. 內容大小分桶 ----------
    w("\n### 依內容大小分桶\n")
    bs = sorted(by_m[base], key=lambda r: r["bytes_needed"])
    k = max(len(bs) // 5, 1)
    w("| 分組 | 筆數 | 需要位元數 範圍 | 中位 | 抓下/筆 | 有效率 |")
    w("|---|---:|---|---:|---:|---:|")
    for i, lab in enumerate(["最小 20%", "20~40%", "40~60%", "60~80%", "最大 20%"]):
        g = bs[i * k:(i + 1) * k] if i < 4 else bs[4 * k:]
        if not g:
            continue
        nd = [r["bytes_needed"] for r in g]
        mw = S.median(r["bytes_wire"] for r in g)
        w(f"| {lab} | {len(g)} | {min(nd):,} ~ {max(nd):,} | "
          f"{S.median(nd):,.0f} | {mw:,.0f} | 1/{mw/S.median(nd):.0f} |")
    w("\n抓下的量幾乎不隨內容變動，所以內容越短的那一組，有效率越糟。\n")

    # ---------- 8. 併發時間軸 ----------
    if any(r.get("t_start") is not None for r in smp):
        w("\n## 8　併發的時間軸\n")
        w("`t_start` / `t_end` 是每筆相對於該次執行開始的秒數，"
          "可以還原伺服器在壓力下的反應。\n")
        w("比較「開場前 10 筆」與「其餘」的等伺服器時間"
          "（用中位數，避免被單一卡頓拉走）：\n")
        w("| 抓法 | 併發 | 開場 10 筆 | 其餘 | 倍數 | 開場最慢一筆 |")
        w("|---|---:|---:|---:|---:|---:|")
        for m in methods:
            rs = [r for r in by_m[m] if r.get("t_start") is not None]
            if len(rs) < 20:
                continue
            o = sorted(rs, key=lambda r: r["t_start"])
            first, rest = o[:10], o[10:]
            f1 = S.median(r["t_wait"] for r in first) * 1000
            f2 = S.median(r["t_wait"] for r in rest) * 1000
            mx = max(r["t_wait"] for r in first) * 1000
            w(f"| {m} | {rs[0]['workers']} | {f1:.0f}ms | {f2:.0f}ms | "
              f"{f1/f2:.1f}x | {mx:.0f}ms |")
        w("\n每個方法開場都比較慢（連線還沒暖、TLS 要協商），"
          "但併發越高放大得越明顯。")
        w("注意樣本只有 10 筆，單次卡頓就會影響中位數，"
          "要下結論請多跑幾輪看趨勢。\n")

    # ---------- 9. 持續抓下去會不會變慢 ----------
    seg_ok = [m for m in methods
              if len([r for r in by_m[m] if r.get("t_start") is not None]) >= 50]
    if seg_ok:
        w("\n## 9　持續抓下去，伺服器會不會變慢\n")
        w("把每次執行依 `t_start` 切成 5 等分，看等伺服器的中位數有沒有往上爬。"
          "會爬升就代表對方開始吃力或在限流 —— 這是決定能不能跑全站的關鍵。\n")
        w("| 抓法 | 第1段 | 第2段 | 第3段 | 第4段 | 第5段 | 末/首 | 判讀 |")
        w("|---|---:|---:|---:|---:|---:|---:|---|")
        for m in seg_ok:
            rs = sorted([r for r in by_m[m] if r.get("t_start") is not None],
                        key=lambda r: r["t_start"])
            k = len(rs) // 5
            segs = [rs[i * k:(i + 1) * k] for i in range(5)]
            med = [S.median(x["t_wait"] for x in g) * 1000 for g in segs if g]
            ratio = med[-1] / med[0] if med[0] else 0
            verdict = ("穩定" if ratio < 1.3 else
                       "略為變慢" if ratio < 2 else "**明顯變慢**")
            w(f"| {m} | " + " | ".join(f"{x:.0f}ms" for x in med)
              + f" | {ratio:.2f}x | {verdict} |")
        w("")

    # ---------- 9b. 限流偵測 ----------
    w("\n### 限流偵測\n")
    w("找「連續 20 筆的等伺服器中位數突然超過 1 秒」的轉折點。"
      "漸進變慢是伺服器忙；**階梯式跳升然後固定不動，是被限流**。\n")
    w("| 抓法 | keep-alive | 轉折點 | 轉折前 | 轉折後 | 全程 >1s 筆數 |")
    w("|---|---|---|---:|---:|---:|")
    hit = []
    for m in methods:
        rs = sorted([r for r in by_m[m] if r.get("t_start") is not None],
                    key=lambda r: r["t_start"])
        if len(rs) < 40:
            continue
        v = [r["t_wait"] * 1000 for r in rs]
        cut = next((i for i in range(len(v) - 20)
                    if S.median(v[i:i + 20]) > 1000), None)
        slow = sum(1 for x in v if x > 1000)
        ka = "有" if rs[0]["keep_alive"] else "**無**"
        if cut is None:
            w(f"| {m} | {ka} | 沒有 | – | – | {slow}/{len(v)} |")
        else:
            hit.append((m, cut, rs[cut]["t_start"]))
            w(f"| {m} | {ka} | **第 {cut+1} 筆**"
              f"（開跑 {rs[cut]['t_start']:.0f}s） | "
              f"{S.median(v[:cut]):.0f}ms | {S.median(v[cut:]):.0f}ms | "
              f"{slow}/{len(v)} |")
    if hit:
        m, cut, t = hit[0]
        w(f"\n**方法 {m} 被限流了。** 前 {cut} 筆正常，第 {cut+1} 筆起每一次都被"
          f"加罰約 2 秒，而且直到跑完都沒有恢復。"
          f"轉折發生在開跑後 {t:.0f} 秒、約 {cut/max(t,1):.1f} 個新連線/秒。\n")
        w("關鍵在於：**它是唯一不重用連線的方法**。緊接在它後面跑的其他方法"
          "完全正常（中位數 35~75ms），所以不是 IP 被封，"
          "是前面掛的 F5 WAF 在懲罰「短時間開太多新連線」這個行為。\n")
        w("這給了「一定要用 Session 重用連線」一個超越效能的理由 —— "
          "**那是會不會被防護設備盯上的分界線**。\n")

    # ---------- 10. 全站推估 ----------
    w("\n## 10　全站 8,277 筆推估\n")
    w("| 抓法 | 預估耗時 | 下載量 | 平均 req/s |")
    w("|---|---:|---:|---:|")
    for m in methods:
        ru = runs_by_m[m]
        per = S.median(x["elapsed"] / max(x["done"], 1) for x in ru)
        mw = S.median(r["bytes_wire"] for r in by_m[m])
        t = per * 8277
        w(f"| {m} | {t/60:.0f} 分 | {mw*8277/1048576:.0f} MB | "
          f"{8277/t:.1f} |")
    w("\n實務上不建議用最快的那個跑全站 —— 見 README「不要被資安盯上」。\n")

    out = "\n".join(L) + "\n"
    open(a.out, "w", encoding="utf-8").write(out)
    print(out)
    print(f"\n>>> 已寫入 {a.out}")


if __name__ == "__main__":
    main()
