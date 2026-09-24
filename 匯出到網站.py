#!/usr/bin/env python3
"""
把抓下來的資料轉成兩個既有網站吃的格式。

    python 匯出到網站.py --target search   # andy939/taipei-faq-search
    python 匯出到網站.py --target ms       # andy939/1999-mystery-shopper

兩個站的資料都是 2026-09-04 從 xlsx 手動匯出的 8,317 筆，這支程式讓它們
可以直接吃爬蟲的資料。

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

OLD_SEARCH = r"D:\ai_work\1150907秘密客\_site_faq\data"
OLD_MS = r"D:\ai_work\1150907秘密客\_site_ms\data"

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


def export_ms(out, old):
    """1999 秘密客站的格式：照機關分片。

        data/index.json  {generated, source, n, linkPrefix, units[], orgs[]}
        orgs[i] = {o: 機關簡稱, f: "org-00",
                   items: [[標題, units的索引, 發布日期, 點閱數, sid, 分類], ...]}
        data/org-NN.json = 那個機關的答案，順序跟 items 對齊

    兩個欄位我們沒有，要沿用舊資料（都用完整 sid 對）：
      點閱數  爬蟲沒抓（要另外打八千多次 GetCounter.ashx）
      分類    17 種人工分類（勞工權益、交通停車…），不是站上的欄位
    """
    f = os.path.join(old, "index.json")
    if not os.path.exists(f):
        raise SystemExit(f"找不到舊索引 {f}")
    with open(f, encoding="utf-8") as fh:
        old_idx = json.load(fh)

    hits, cats, short_tbl = {}, {}, {}
    for og in old_idx["orgs"]:
        for it in og["items"]:
            hits[it[4]] = it[3]                       # sid → 點閱數
            cats[it[4]] = it[5]                       # sid → 分類
            short_tbl[old_idx["units"][it[1]]] = og["o"]   # 機關全名 → 簡稱
    print(f"舊索引：{old_idx['n']} 筆、{len(old_idx['orgs'])} 個機關、"
          f"{len(cats)} 筆有分類")

    recs = C.load_data()
    live = [r for r in recs.values() if not r.get("gone")]
    by_org = {}
    for r in live:
        by_org.setdefault(short_name(r.get("dept") or "", short_tbl), []).append(r)

    units, uidx = [], {}
    orgs, answers, nocat = [], [], 0
    for i, (org, rs) in enumerate(
            sorted(by_org.items(), key=lambda kv: -len(kv[1]))):
        rs.sort(key=lambda r: -hits.get(r["sid"], 0))   # 熱門排前面
        items = []
        for r in rs:
            dept = r.get("dept") or ""
            if dept not in uidx:
                uidx[dept] = len(units)
                units.append(dept)
            cat = cats.get(r["sid"], "")
            nocat += not cat
            items.append([r.get("title") or "", uidx[dept],
                          (r.get("published") or "")[:10],
                          hits.get(r["sid"], 0), r["sid"], cat or "其他綜合"])
        orgs.append({"o": org, "f": f"org-{i:02d}", "items": items})
        answers.append([r.get("answer") or "" for r in rs])

    d = os.path.join(out, "data")
    os.makedirs(d, exist_ok=True)
    idx = {"generated": time.strftime("%Y-%m-%d"),
           "source": "faq-crawler docs/faq.json",
           "n": len(live), "linkPrefix": old_idx["linkPrefix"],
           "units": units, "orgs": orgs}
    with open(os.path.join(d, "index.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))
    for i, ans in enumerate(answers):
        with open(os.path.join(d, f"org-{i:02d}.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump(ans, fh, ensure_ascii=False, separators=(",", ":"))

    size = sum(os.path.getsize(os.path.join(d, x)) for x in os.listdir(d))
    print(f"\n→ {d}")
    print(f"  {len(live)} 筆、{len(orgs)} 個機關、合計 {size/1048576:.1f} MB")
    print(f"  前五大：" + "、".join(f"{o['o']}({len(o['items'])})" for o in orgs[:5]))
    print(f"  沿用到點閱數 {sum(1 for o in orgs for it in o['items'] if it[3])} 筆，"
          f"沒有分類而歸到「其他綜合」的 {nocat} 筆")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=("search", "ms"), default="search",
                    help="search＝FAQ 搜尋站，ms＝1999 秘密客")
    ap.add_argument("--out", default="")
    ap.add_argument("--old", default="", help="舊的 data 資料夾")
    a = ap.parse_args()
    if a.target == "ms":
        a.old = a.old or OLD_MS
        return export_ms(a.out or "_site_ms_out", a.old)
    a.old = a.old or OLD_SEARCH
    a.out = a.out or "_site_faq_out"

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
