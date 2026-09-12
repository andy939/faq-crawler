#!/usr/bin/env python3
"""
把歷次抓取的測量資料整理成 report.md。

    python report.py            # → report.md
    python report.py --out 檔名.md

讀 docs/runs.csv（每次執行的彙總）和 exports/*.csv（逐筆原始測量）。
抓法比較還沒結束，所以這份報表是跟著資料長的 —— 每跑一次新的測試重跑就好。
"""

import argparse
import csv
import glob
import os
import statistics as st
import time
from collections import defaultdict

import crawl as C


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def table(head, rows, align=None):
    """產生 markdown 表格。align 用 'r' 標記靠右的欄。"""
    align = align or ""
    sep = ["---:" if i < len(align) and align[i] == "r" else "---"
           for i in range(len(head))]
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join(sep) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report.md")
    a = ap.parse_args()

    runs = read(C.RUNS)
    meta = C.load_meta()
    recs = C.load_data()
    live = [r for r in recs.values() if not r.get("gone")]

    L = [f"# 抓取測量報表",
         "",
         f"產生時間：{time.strftime('%Y-%m-%d %H:%M')}　"
         f"資料：{len(runs)} 次執行紀錄、{len(recs)} 筆問答",
         ""]

    # ---- 現況 ----
    L += ["## 現況", "",
          table(["項目", "數字"], [
              ["官網筆數", meta.get("site_total", "—")],
              ["已下載", meta.get("got", len(live))],
              ["完整性", "★ 一致" if meta.get("got") == meta.get("site_total")
               else f"差 {num(meta.get('site_total')) - num(meta.get('got')):.0f} 筆"],
              ["有內容", meta.get("with_content", "—")],
              ["已下架保留", meta.get("gone_total", 0)],
              ["上次抓取", f"{meta.get('last_run','—')}"
                          f"（{meta.get('last_mode','')}，{meta.get('last_where','')}）"],
          ], "r"), ""]

    # ---- 抓法比較 ----
    by = defaultdict(list)
    for r in runs:
        if num(r.get("處理筆數")) >= 5:       # 太少筆沒有代表性
            by[r.get("抓法", "?")].append(r)
    if by:
        L += ["## 抓法比較", "",
              "每一列是同一種抓法所有測試的中位數。"
              "「等伺服器」是送出到收到第一個位元組，"
              "「傳輸」是把整頁收完 —— 兩段分開才看得出慢在哪裡。", ""]
        rows = []
        for k in sorted(by):
            g = by[k]

            def med(col):
                v = [num(x.get(col)) for x in g if x.get(col) not in ("", None)]
                return st.median(v) if v else 0
            rows.append([k, len(g), f"{med('處理筆數'):.0f}",
                         f"{med('每筆毫秒'):.0f} ms",
                         f"{med('等伺服器ms'):.0f} ms",
                         f"{med('傳輸ms'):.0f} ms",
                         f"{med('解析ms'):.2f} ms",
                         f"{med('壓縮率%'):.0f}%",
                         f"{med('有效率%'):.2f}%"])
        L += [table(["抓法", "測試次數", "筆數", "每筆", "等伺服器", "傳輸",
                     "解析", "壓縮率", "有效率"], rows, "rrrrrrrrr"), ""]

    # ---- 逐筆測量 ----
    files = sorted(glob.glob(os.path.join(C.EXPORTS, "*.csv")))
    samples = []
    for p in files:
        samples += read(p)
    if samples:
        wire = [num(s["抓下bytes"]) for s in samples]
        raw = [num(s["原始bytes"]) for s in samples]
        need = [num(s["需要bytes"]) for s in samples]
        L += ["## 位元效率", "",
              f"逐筆測量共 {len(samples):,} 筆（{len(files)} 個檔案）。", "",
              table(["項目", "中位", "最小", "最大"], [
                  ["一頁原始 HTML", f"{st.median(raw)/1024:.0f} KB",
                   f"{min(raw)/1024:.0f} KB", f"{max(raw)/1024:.0f} KB"],
                  ["gzip 後抓下", f"{st.median(wire)/1024:.0f} KB",
                   f"{min(wire)/1024:.0f} KB", f"{max(wire)/1024:.0f} KB"],
                  ["真正需要的欄位", f"{st.median(need):.0f} B",
                   f"{min(need):.0f} B", f"{max(need):.0f} B"],
              ], "rrr"), "",
              f"**有效率約 {sum(need)/sum(wire)*100:.2f}%** —— "
              f"抓下來的每 100 個位元組裡，只有不到一個是真的要留的。"
              f"每頁固定成本（版型骨架）跟內容長短幾乎無關。", ""]

        # 等伺服器 vs 傳輸
        w = [num(s["等伺服器ms"]) for s in samples]
        t = [num(s["傳輸ms"]) for s in samples]
        pa = [num(s["解析ms"]) for s in samples]
        L += ["## 時間花在哪", "",
              table(["階段", "中位", "p95", "佔比"], [
                  ["等伺服器", f"{st.median(w):.0f} ms",
                   f"{sorted(w)[int(len(w)*.95)]:.0f} ms",
                   f"{sum(w)/(sum(w)+sum(t)+sum(pa))*100:.0f}%"],
                  ["傳輸", f"{st.median(t):.0f} ms",
                   f"{sorted(t)[int(len(t)*.95)]:.0f} ms",
                   f"{sum(t)/(sum(w)+sum(t)+sum(pa))*100:.0f}%"],
                  ["解析", f"{st.median(pa):.2f} ms",
                   f"{sorted(pa)[int(len(pa)*.95)]:.2f} ms",
                   f"{sum(pa)/(sum(w)+sum(t)+sum(pa))*100:.1f}%"],
              ], "rrr"), "",
              "解析時間完全可以忽略，瓶頸不在 Python。", ""]

    # ---- 歷次執行 ----
    if runs:
        L += ["## 最近 20 次執行", ""]
        cols = ["時間", "來源", "模式", "抓法", "處理筆數", "耗時秒",
                "每筆毫秒", "被擋", "備註"]
        L += [table(cols, [[r.get(c, "") for c in cols] for r in runs[-20:][::-1]],
                    "  rrrrr"), ""]

    # ---- 限流 ----
    blocked = [r for r in runs if num(r.get("被擋")) > 0]
    if blocked:
        L += ["## 被擋過的紀錄", "",
              "被擋＝收到 3 KB 錯誤頁或 403/429/503。"
              "那種錯誤頁 HTTP 狀態碼仍然是 200，所以是用頁面大小判斷的。", "",
              table(["時間", "來源", "抓法", "被擋次數", "每筆毫秒", "備註"],
                    [[r.get("時間"), r.get("來源"), r.get("抓法"),
                      r.get("被擋"), r.get("每筆毫秒"), r.get("備註", "")]
                     for r in blocked[-15:][::-1]], "   rr"), ""]

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L))
    print(f"→ {a.out}（{len(L)} 行）")
    if by:
        print(f"  抓法 {len(by)} 種、執行 {len(runs)} 次、逐筆測量 {len(samples):,} 筆")


if __name__ == "__main__":
    main()
