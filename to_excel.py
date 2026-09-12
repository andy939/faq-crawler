#!/usr/bin/env python3
"""
把所有資料輸出成一個 Excel 檔（多工作表 + 圖表 + 名詞說明）。

    python to_excel.py                  # 存成 常見問答_YYYYMMDD.xlsx
    python to_excel.py --out 檔名.xlsx
    python to_excel.py --html           # 「問答」那頁多一欄原始 HTML

需要 openpyxl：pip install openpyxl

讀的是 docs/ 底下那幾個檔和 exports/ 的逐筆測量，不碰網路、不碰舊的 faq.db。
"""

import argparse
import csv
import glob
import json
import os
import re
import time
from collections import Counter

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import crawl as C

# openpyxl 不收控制字元，內文偶爾會混到 \x00-\x1f，不濾掉匯出就會炸
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
HEAD_FILL = PatternFill("solid", fgColor="1A6A5A")
HEAD_FONT = Font(bold=True, color="FFFFFF")


def clean(v):
    if isinstance(v, str):
        v = CTRL.sub("", v)
        return v[:32000]          # Excel 單格上限 32767 字元
    return v


def sheet(wb, title, header, rows, widths=None, freeze="A2"):
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in rows:
        ws.append([clean(x) for x in r])
    for c in range(1, len(header) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = (
            widths[c - 1] if widths and c <= len(widths) else 14)
    ws.freeze_panes = freeze
    ws.auto_filter.ref = ws.dimensions
    return ws


def read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--html", action="store_true", help="多輸出一欄原始 HTML")
    a = ap.parse_args()
    out = a.out or f"常見問答_{time.strftime('%Y%m%d_%H%M')}.xlsx"

    recs = C.load_data()
    htmls = C.load_html() if a.html else {}
    meta = C.load_meta()
    live = [r for r in recs.values() if not r.get("gone")]
    today = time.strftime("%Y-%m-%d")

    wb = Workbook()

    # ---------- 說明 ----------
    ws = wb.active
    ws.title = "說明"
    gloss = [
        ("項目", "內容"),
        ("資料來源", "臺北市政府全球資訊網「常見問答」"
                     "https://www.gov.taipei/News.aspx?n=EEC70A4186D4C828"
                     "&sms=87415A8B9CE81B16"),
        ("匯出時間", time.strftime("%Y-%m-%d %H:%M")),
        ("官網筆數", meta.get("site_total", "")),
        ("已下載", meta.get("got", len(live))),
        ("有內容", meta.get("with_content", "")),
        ("已下架保留", meta.get("gone_total", 0)),
        ("上次抓取", f"{meta.get('last_run','')}（{meta.get('last_mode','')}，"
                     f"{meta.get('last_where','')}）"),
        ("", ""),
        ("名詞", "意思"),
        ("實際耗時（牆鐘）",
         "從開始到結束，牆上的時鐘走了多久。就是你實際等待的時間。"
         "併發時多個請求同時進行，所以會小於「所有請求時間加總」。"),
        ("等伺服器",
         "送出請求 → 收到第一個位元組。包含建立連線（TCP/TLS）"
         "和伺服器組出頁面的時間。也叫 TTFB。"),
        ("傳輸", "收到第一個位元組 → 把整個頁面收完。純粹是資料流過網路的時間。"),
        ("抓下 bytes", "真正流過網路的位元組（gzip 壓縮後）。"),
        ("原始 bytes", "解壓後的完整 HTML。關掉 gzip 時兩者相同。"),
        ("需要 bytes",
         "發布時間＋更新時間＋標題＋發布機關＋內容 這幾個欄位的 UTF-8 位元組。"
         "中文一字 3 bytes。"),
        ("有效率", "需要 ÷ 抓下。這站約 1%：一頁 197 KB 的版型只為了幾百 bytes 的答案。"),
        ("壓縮率", "抓下 ÷ 原始。這站約 28%。"),
        ("被擋", "收到 3 KB 錯誤頁或 403/429/503 的次數。"
                 "那種錯誤頁 HTTP 狀態碼仍然是 200，所以是用頁面大小判斷的。"),
        ("", ""),
        ("抓法", "說明"),
    ] + [(f"{k}　{v[0]}", v[1]) for k, v in C.METHODS.items()] + [
        ("", ""),
        ("工作表", "內容"),
        ("問答", "全部問答，一筆一列。這是資料本體"),
        ("維護建議", "要發給各局處的待修正清單，一列一個問題"),
        ("機關統計", "各機關的筆數與維護狀況"),
        ("年份統計", "依發布年份"),
        ("執行紀錄", "每次抓取的彙總（耗時、每筆、壓縮率、有效率…）"),
        ("異動紀錄", "逐筆的新增／異動／下架／復原"),
        ("逐筆測量", "每次執行 × 每一筆的原始測量，可直接做樞紐分析"),
        ("圖表", "抓法比較、年份分布、機關前 15"),
    ]
    for r in gloss:
        ws.append(list(r))
    for row in range(1, ws.max_row + 1):
        ws.cell(row=row, column=1).font = Font(bold=True)
        ws.cell(row=row, column=2).alignment = Alignment(wrap_text=True,
                                                         vertical="top")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 95

    # ---------- 問答 ----------
    hdr = ["sid", "標題", "發布機關", "發布日期", "更新時間", "檢視時間",
           "下版日期", "維護單位", "內容", "內容字數", "附件數", "附件",
           "類型", "已下架", "網址"]
    if a.html:
        hdr.append("原始HTML")
    rows = []
    for r in sorted(recs.values(), key=lambda x: (x.get("published") or ""),
                    reverse=True):
        row = [r["sid"], r["title"], r["dept"], (r.get("published") or "")[:10],
               r.get("updated", ""), r.get("reviewed", ""), r.get("expire", ""),
               r.get("maintainer", ""), r.get("answer", ""),
               len(r.get("answer") or ""), len(r.get("files") or []),
               " | ".join(f["name"] for f in (r.get("files") or [])),
               "外部連結" if r.get("kind") == "external" else "站內",
               r.get("gone", ""), r["url"]]
        if a.html:
            row.append(htmls.get(r["sid"], ""))
        rows.append(row)
    sheet(wb, "問答", hdr, rows,
          widths=[18, 52, 22, 12, 17, 17, 12, 22, 70, 10, 8, 30, 10, 11, 60])

    # ---------- 維護建議 ----------
    titles = Counter((r.get("title") or "").strip() for r in live)
    old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 365 * 86400))
    soon = time.strftime("%Y-%m-%d", time.localtime(time.time() + 90 * 86400))
    fix = []
    for r in live:
        exp = (r.get("expire") or "")[:10]
        chk = (r.get("reviewed") or r.get("updated") or r.get("published") or "")[:10]

        def add(problem, why):
            fix.append([r.get("dept") or "（未填）", problem, why, r["title"],
                        (r.get("published") or "")[:10], exp, chk, r["url"]])
        if exp and exp < today:
            add("已過期還掛著", f"下版日期 {exp} 已過")
        if not exp:
            add("沒設下版日期", "不會自動下架，需要人工盯")
        if not chk or chk < old:
            add("一年以上沒檢視", f"最後異動 {chk or '不明'}")
        if r.get("kind") != "external" and not r.get("answer") \
                and not r.get("files"):
            add("沒有內容", "內文空白且沒有附件")
        if r.get("kind") == "external":
            add("連到外部網站", "內容不在市府網站，連結可能失效")
        if titles[(r.get("title") or "").strip()] > 1:
            add("標題重複", f"同樣標題有 {titles[(r.get('title') or '').strip()]} 筆")
        if exp and today <= exp <= soon:
            add("90 天內到期", f"下版日期 {exp}")
    fix.sort(key=lambda x: (x[0], x[1]))
    sheet(wb, "維護建議",
          ["機關", "問題", "說明", "標題", "發布日期", "下版日期", "最後檢視", "網址"],
          fix, widths=[24, 16, 30, 52, 12, 12, 12, 60])

    # ---------- 機關統計 ----------
    by_dept = {}
    for r in live:
        d = by_dept.setdefault(r.get("dept") or "（未填）",
                               {"n": 0, "content": 0, "files": 0, "noexp": 0})
        d["n"] += 1
        d["content"] += 1 if (r.get("answer") or r.get("files")) else 0
        d["files"] += len(r.get("files") or [])
        d["noexp"] += 0 if r.get("expire") else 1
    dept_rows = [[k, v["n"], v["content"], v["files"], v["noexp"],
                  sum(1 for f in fix if f[0] == k)]
                 for k, v in sorted(by_dept.items(),
                                    key=lambda kv: -kv[1]["n"])]
    sheet(wb, "機關統計",
          ["機關", "筆數", "有內容", "附件數", "沒設下版日期", "待修正項目"],
          dept_rows, widths=[30, 10, 10, 10, 14, 12])

    # ---------- 年份統計 ----------
    yr = Counter((r.get("published") or "")[:4] for r in live if r.get("published"))
    year_rows = [[y, n] for y, n in sorted(yr.items(), reverse=True)]
    sheet(wb, "年份統計", ["年份", "筆數"], year_rows, widths=[10, 10])

    # ---------- 執行紀錄 ----------
    h, rows = read_csv(C.RUNS)
    if h:
        num = {"站上筆數", "已下載", "缺", "新增", "異動", "下架", "復原",
               "處理筆數", "請求數", "清單請求", "內文請求", "被擋", "錯誤",
               "耗時秒", "每筆毫秒", "請求每秒", "等伺服器ms", "傳輸ms", "解析ms",
               "抓下MB", "原始MB", "需要KB", "壓縮率%", "有效率%",
               "延遲p50ms", "延遲p95ms", "併發", "限速ms"}
        idx = [i for i, c in enumerate(h) if c in num]
        conv = []
        for r in rows[::-1]:
            r = list(r)
            for i in idx:
                if i < len(r) and r[i] not in ("", None):
                    try:
                        r[i] = float(r[i]) if "." in r[i] else int(r[i])
                    except ValueError:
                        pass
            conv.append(r)
        sheet(wb, "執行紀錄", h, conv, widths=[16, 8, 12, 26] + [10] * 30)

    # ---------- 異動紀錄 ----------
    h, rows = read_csv(C.CHANGES)
    if h:
        sheet(wb, "異動紀錄", h, rows[::-1],
              widths=[16, 8, 22, 12, 24, 52, 18, 60])

    # ---------- 逐筆測量 ----------
    files = sorted(glob.glob(os.path.join(C.EXPORTS, "*.csv")))
    allrows, head = [], []
    for p in files[-20:]:                 # 最近 20 次執行就夠了，再多 Excel 會很慢
        h, rows = read_csv(p)
        if not h:
            continue
        head = ["來源檔"] + h
        for r in rows:
            allrows.append([os.path.basename(p)] + r)
    if head:
        for r in allrows:
            for i, c in enumerate(head):
                if c in ("抓下bytes", "原始bytes", "需要bytes", "內容字數",
                         "附件數", "等伺服器ms", "傳輸ms", "解析ms", "總計ms",
                         "seq", "併發"):
                    try:
                        r[i] = float(r[i]) if "." in str(r[i]) else int(r[i])
                    except (ValueError, TypeError):
                        pass
        sheet(wb, "逐筆測量", head, allrows, widths=[30, 6, 18, 8, 8, 6, 46, 22])

    # ---------- 圖表 ----------
    ws = wb.create_sheet("圖表")
    dat = wb.create_sheet("圖表資料")
    dat.sheet_state = "hidden"

    # 抓法比較：從執行紀錄算每種抓法的每筆毫秒中位數
    h, rows = read_csv(C.RUNS)
    if h and "抓法" in h:
        mi, si, ci = h.index("抓法"), h.index("每筆毫秒"), h.index("處理筆數")
        agg = {}
        for r in rows:
            try:
                if int(r[ci]) < 5:
                    continue            # 太少筆的不列入，沒有代表性
                agg.setdefault(r[mi], []).append(float(r[si]))
            except (ValueError, IndexError):
                continue
        dat.append(["抓法", "每筆毫秒（中位）", "次數"])
        for k, v in sorted(agg.items()):
            v.sort()
            dat.append([k, v[len(v) // 2], len(v)])
        if dat.max_row > 1:
            ch = BarChart()
            ch.title = "各抓法每筆耗時（中位數，毫秒）"
            ch.y_axis.title, ch.x_axis.title = "毫秒/筆", "抓法"
            ch.add_data(Reference(dat, min_col=2, min_row=1, max_row=dat.max_row),
                        titles_from_data=True)
            ch.set_categories(Reference(dat, min_col=1, min_row=2,
                                        max_row=dat.max_row))
            ch.width, ch.height = 22, 10
            ws.add_chart(ch, "A1")

    r0 = dat.max_row + 2
    dat.cell(row=r0, column=1, value="年份")
    dat.cell(row=r0, column=2, value="筆數")
    for i, (y, n) in enumerate(sorted(yr.items()), 1):
        dat.cell(row=r0 + i, column=1, value=y)
        dat.cell(row=r0 + i, column=2, value=n)
    ch = LineChart()
    ch.title = "各年發布筆數"
    ch.add_data(Reference(dat, min_col=2, min_row=r0, max_row=r0 + len(yr)),
                titles_from_data=True)
    ch.set_categories(Reference(dat, min_col=1, min_row=r0 + 1,
                                max_row=r0 + len(yr)))
    ch.width, ch.height = 22, 10
    ws.add_chart(ch, "A22")

    r1 = dat.max_row + 2
    dat.cell(row=r1, column=1, value="機關")
    dat.cell(row=r1, column=2, value="筆數")
    for i, row in enumerate(dept_rows[:15], 1):
        dat.cell(row=r1 + i, column=1, value=row[0])
        dat.cell(row=r1 + i, column=2, value=row[1])
    ch = BarChart()
    ch.type, ch.title = "bar", "發布最多的 15 個機關"
    ch.add_data(Reference(dat, min_col=2, min_row=r1,
                          max_row=r1 + min(15, len(dept_rows))),
                titles_from_data=True)
    ch.set_categories(Reference(dat, min_col=1, min_row=r1 + 1,
                                max_row=r1 + min(15, len(dept_rows))))
    ch.width, ch.height = 22, 12
    ws.add_chart(ch, "A43")

    wb.save(out)
    size = os.path.getsize(out) / 1048576
    print(f"→ {out}（{size:.1f} MB）")
    print(f"  工作表：{'、'.join(wb.sheetnames)}")
    print(f"  問答 {len(rows) if False else len(recs)} 筆、"
          f"待修正 {len(fix)} 項、機關 {len(by_dept)} 個")


if __name__ == "__main__":
    main()
