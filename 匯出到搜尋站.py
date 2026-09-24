#!/usr/bin/env python3
"""
把抓下來的資料轉成「臺北市 FAQ 搜尋站」（andy939/taipei-faq-search）的格式。

    python 匯出到搜尋站.py                    # 輸出到 _site_faq_out/data/
    python 匯出到搜尋站.py --out 別的資料夾
    python 匯出到搜尋站.py --old 舊的 data 資料夾

那個站的資料長這樣（index 一個檔、答案切片存）：

    data/faq-index.json   {generated, source, n, orgs, shard, items[]}
    data/faq-a-00.json    [{"a": 答案}, ...]   每片 347 筆

    items[i] = [標題, 機關簡稱, 機關全名, 發布日期, 點閱數,
                sid前8碼, 網址, 第幾片, 片內第幾筆]

兩件事需要舊資料：

  點閱數    我們的爬蟲沒抓（要另外打八千多次 GetCounter.ashx），
            所以沿用舊索引的數字，新進來的補 0。不然熱門排序會全毀。
  機關簡稱  「臺北市政府勞動局勞動基準科」→「勞動局」。優先用舊索引裡
            既有的對照，沒有的才用規則推，這樣跟原本的分類一致。
"""

import argparse
import json
import os
import re
import time

import crawl as C

SHARD = 347                     # 每片幾筆，沿用原本的設定
DEFAULT_OLD = r"D:\ai_work\1150907秘密客\_site_faq\data"


def load_old(path):
    """從舊的 faq-index.json 撈出點閱數與機關簡稱對照。"""
    f = os.path.join(path, "faq-index.json")
    if not os.path.exists(f):
        print(f"（找不到舊索引 {f}，點閱數一律 0、機關簡稱全部用規則推）")
        return {}, {}
    with open(f, encoding="utf-8") as fh:
        idx = json.load(fh)
    hits, short = {}, {}
    for it in idx.get("items", []):
        if len(it) > 6:
            hits[it[5]] = it[4]          # sid 前 8 碼 → 點閱數
            if it[2]:
                short[it[2]] = it[1]     # 機關全名 → 簡稱
    print(f"舊索引：{len(idx.get('items', []))} 筆，"
          f"點閱數 {len(hits)} 筆、機關對照 {len(short)} 組")
    return hits, short


# 規則：去掉「臺北市政府」「臺北市立」「臺北市」開頭，再切到第一個
# 局／處／所／會／中心／館／署／院 為止。舊索引沒有的機關才會走到這裡。
PREFIX = re.compile(r"^(臺北市政府|臺北市立|臺北市)")
CUT = re.compile(r"^(.*?(?:委員會|中心|局|處|所|會|館|署|院|大隊|分局))")


def short_name(full, table):
    if not full:
        return ""
    if full in table:
        return table[full]
    s = PREFIX.sub("", full)
    m = CUT.match(s)
    return m.group(1) if m else s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site_faq_out")
    ap.add_argument("--old", default=DEFAULT_OLD, help="舊的 data 資料夾")
    a = ap.parse_args()

    hits, short_tbl = load_old(a.old)
    recs = C.load_data()
    live = [r for r in recs.values() if not r.get("gone")]
    # 熱門排在前面（跟原本的站一致）；沒有點閱數的照發布日期新到舊
    live.sort(key=lambda r: (-hits.get(r["sid"][:8], 0),
                             (r.get("published") or "")[:10]), reverse=False)
    live.sort(key=lambda r: hits.get(r["sid"][:8], 0), reverse=True)

    items, shards, orgs = [], [], {}
    for i, r in enumerate(live):
        sh, off = divmod(i, SHARD)
        while len(shards) <= sh:
            shards.append([])
        shards[sh].append({"a": r.get("answer") or ""})
        org = short_name(r.get("dept") or "", short_tbl)
        orgs[org] = orgs.get(org, 0) + 1
        items.append([
            r.get("title") or "", org, r.get("dept") or "",
            (r.get("published") or "")[:10],
            hits.get(r["sid"][:8], 0), r["sid"][:8], r.get("url") or "",
            sh, off,
        ])

    out = os.path.join(a.out, "data")
    os.makedirs(out, exist_ok=True)
    idx = {
        "generated": time.strftime("%Y-%m-%d"),
        "source": "faq-crawler docs/faq.json",
        "n": len(items),
        "orgs": sorted(orgs.items(), key=lambda kv: -kv[1]),
        "shard": SHARD,
        "items": items,
    }
    with open(os.path.join(out, "faq-index.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    for i, sh in enumerate(shards):
        with open(os.path.join(out, f"faq-a-{i:02d}.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(sh, f, ensure_ascii=False, separators=(",", ":"))

    size = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out))
    got_hits = sum(1 for it in items if it[4])
    print(f"\n→ {out}")
    print(f"  {len(items)} 筆、{len(shards)} 個分片、合計 {size/1048576:.1f} MB")
    print(f"  機關 {len(orgs)} 個，前五名："
          + "、".join(f"{k}({v})" for k, v in idx["orgs"][:5]))
    print(f"  沿用到點閱數的 {got_hits} 筆，新資料沒有點閱數的 {len(items)-got_hits} 筆")


if __name__ == "__main__":
    main()
