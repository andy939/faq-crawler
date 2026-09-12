#!/usr/bin/env python3
"""
每日增量更新 —— 只抓新增與異動，不重跑全站。

    python3 update.py              # 日常更新（約 3~10 次請求，幾秒完成）
    python3 update.py --refresh 300   # 順便輪流複查 300 筆舊資料
    python3 update.py --dry-run    # 只看會做什麼，不寫入
    python3 update.py --force      # 忽略探測結果硬跑（不建議）

做兩件事：
  1. 新增：抓清單前幾頁，比對 sid，只有沒看過的才進去抓內文。
     站上每天大約新增 2~3 筆，所以通常是「2 次清單 + 3 次內文」。
  2. 異動：清單看不到「資料更新」時間，所以舊資料的修改偵測不到。
     用 --refresh N 輪流複查最久沒查的 N 筆（依 fetched_at 排序），
     每天 300 筆的話約一個月全站輪一輪。

開跑前會先探測伺服器狀態，發現已經在被限流就直接跳過這次，不硬衝。
"""

import argparse
import json
import os
import sqlite3
import sys
import time

import requests

import faqlib as F

DB = F.DB
LOG = os.path.join(F.HERE, "update.log")

INTERVAL_MS = 400        # 每筆間隔，比全站抓取更保守
PROBE_N = 5              # 開跑前探測幾筆
PROBE_LIMIT_MS = 800     # 探測中位數超過這個就跳過本次
PAGE_SIZE = F.MAX_PAGE_SIZE

# ---- 資料庫 -----------------------------------------------------------------
def db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ok INTEGER,
        probe_ms REAL, list_pages INTEGER, new_items INTEGER,
        changed INTEGER, refreshed INTEGER, reqs INTEGER,
        elapsed REAL, note TEXT)""")
    con.commit()
    return con


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---- 主流程 -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", type=int, default=0,
                    help="順便輪流複查最久沒查的 N 筆（偵測舊資料被修改）")
    ap.add_argument("--dry-run", action="store_true", help="只看不寫")
    ap.add_argument("--force", action="store_true", help="忽略探測結果硬跑")
    ap.add_argument("--max-pages", type=int, default=5,
                    help="清單最多翻幾頁（預設 5，即最近 1000 筆）")
    a = ap.parse_args()

    t0 = time.perf_counter()
    con = db()
    known = {r[0] for r in con.execute("SELECT sid FROM faq")}
    s = F.new_session()
    rate = F.RateLimiter(INTERVAL_MS)
    reqs = [0]

    def get(url):
        rate.wait()
        r = s.get(url, timeout=30)
        r.encoding = "utf-8"
        reqs[0] += 1
        return r



    # --- 探測 ---------------------------------------------------------------
    probe_urls = [r[0] for r in con.execute(
        "SELECT url FROM faq WHERE kind='internal' ORDER BY no LIMIT ?",
        (PROBE_N,))]
    lat = []
    for u in probe_urls:
        t = time.perf_counter()
        get(u)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    probe = lat[len(lat) // 2] if lat else 0.0
    log(f"探測 {len(lat)} 筆，中位 {probe:.0f}ms")
    if probe > PROBE_LIMIT_MS and not a.force:
        log(f"！伺服器正在限流（>{PROBE_LIMIT_MS}ms），本次跳過。"
            f"稍後再跑，或加 --force 硬跑。")
        con.execute("INSERT INTO updates (ts,ok,probe_ms,reqs,elapsed,note) "
                    "VALUES (?,0,?,?,?,?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), probe, reqs[0],
                     time.perf_counter() - t0, "被限流，跳過"))
        con.commit()
        sys.exit(2)

    # --- 清單：翻到全部都是看過的為止 --------------------------------------
    fresh, pages = [], 0
    for page in range(1, a.max_pages + 1):
        r = get(f"{F.LIST_URL}&page={page}&PageSize={PAGE_SIZE}")
        rows = F.parse_list(r.text)
        pages += 1
        if not rows:
            break
        unseen = [x for x in rows if x["sid"] not in known]
        fresh += unseen
        log(f"  清單第 {page} 頁：{len(rows)} 筆，其中新的 {len(unseen)} 筆")
        if not unseen:          # 這一整頁都看過了，後面更舊，不用再翻
            break

    new_items = [x for x in fresh if x["kind"] == "internal"]
    ext_new = len(fresh) - len(new_items)

    # --- 要複查的舊資料 -----------------------------------------------------
    stale = []
    if a.refresh > 0:
        stale = [dict(sid=r[0], url=r[1], no=r[2], list_title="", list_dept="",
                      list_date="", kind="internal")
                 for r in con.execute(
                     "SELECT sid,url,no FROM faq WHERE kind='internal' "
                     "ORDER BY fetched_at LIMIT ?", (a.refresh,))]

    todo = new_items + stale
    log(f"新增 {len(new_items)} 筆"
        + (f"（另有 {ext_new} 筆外部連結，跳過）" if ext_new else "")
        + (f"　複查 {len(stale)} 筆" if stale else ""))

    if a.dry_run:
        for x in new_items[:20]:
            log(f"    [新] {x['list_date']}  {x['list_title'][:40]}")
        log(f"（dry-run，沒有寫入）預計還要 {len(todo)} 次請求")
        return

    if not todo:
        log(f"沒有新增，完成。共 {reqs[0]} 次請求，"
            f"{time.perf_counter()-t0:.1f} 秒")
        con.execute("INSERT INTO updates "
                    "(ts,ok,probe_ms,list_pages,new_items,changed,refreshed,"
                    "reqs,elapsed,note) VALUES (?,1,?,?,0,0,0,?,?,'無新增')",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), probe, pages,
                     reqs[0], time.perf_counter() - t0))
        con.commit()
        return

    # --- 逐筆抓 -------------------------------------------------------------
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    changed, added = 0, 0
    for it in todo:
        try:
            r = get(it["url"])
        except requests.RequestException as e:
            log(f"    ! {it['sid']} 抓取失敗：{type(e).__name__}")
            continue
        if F.is_blocked(r):
            log(f"    ! {it['sid']} 拿到錯誤頁（被擋），跳過不覆蓋")
            continue
        d = F.parse_detail(r.text)
        # 用「有沒有標題」判斷是不是真的抓到（faqlib.page_ok，跟 app.py 同一套）。
        # 不能用「有沒有內文」—— 有些問答的答案就是一份 PDF 附件，
        # 也有些站上本來只建了標題沒填內容，那些都不是失敗，
        # 用內文判斷的話它們永遠進不了資料庫、也永遠不會被更新。
        if not F.page_ok(d):
            log(f"    ! {it['sid']} 頁面不正常（沒有標題），跳過不覆蓋")
            continue
        pub = d["published"] or it["list_date"]
        rec = {
            "sid": it["sid"], "no": it["no"], "url": it["url"],
            "kind": "internal",
            "title": d["title"] or it["list_title"],
            "dept": d["dept"] or it["list_dept"],
            "date": it["list_date"] or pub, "published": pub,
            "updated": d["updated"], "published_iso": F.roc_to_iso(pub),
            "answer": d["body"], "fetched_at": now,
            "answer_html": d["body_html"] or "",
            "reviewed": d["reviewed"], "expire": d["expire"],
            "maintainer": d["maintainer"],
            "files": json.dumps(d.get("files") or [], ensure_ascii=False),
            "file_count": len(d.get("files") or []),
        }
        rec["bytes_needed"] = F.needed_bytes(rec)
        old = con.execute(
            "SELECT updated, answer FROM faq WHERE sid=?", (it["sid"],)).fetchone()
        con.execute("""
            INSERT INTO faq (sid,no,title,dept,date,url,kind,answer,answer_html,
                             published,updated,published_iso,fetched_at,
                             bytes_needed,reviewed,expire,maintainer,
                             files,file_count)
            VALUES (:sid,:no,:title,:dept,:date,:url,:kind,:answer,:answer_html,
                    :published,:updated,:published_iso,:fetched_at,
                    :bytes_needed,:reviewed,:expire,:maintainer,
                    :files,:file_count)
            ON CONFLICT(sid) DO UPDATE SET
              no=excluded.no, title=excluded.title, dept=excluded.dept,
              date=excluded.date, answer=excluded.answer,
              answer_html=excluded.answer_html,
              published=excluded.published, updated=excluded.updated,
              published_iso=excluded.published_iso,
              fetched_at=excluded.fetched_at,
              bytes_needed=excluded.bytes_needed,
              reviewed=excluded.reviewed, expire=excluded.expire,
              maintainer=excluded.maintainer,
              files=excluded.files, file_count=excluded.file_count
            WHERE excluded.title IS NOT NULL AND excluded.title <> ''""", rec)
        if old is None:
            added += 1
            log(f"    [新] {pub}  {rec['title'][:44]}")
        elif old[0] != rec["updated"] or old[1] != rec["answer"]:
            changed += 1
            log(f"    [改] {rec['updated']}  {rec['title'][:44]}")
    con.commit()

    el = time.perf_counter() - t0
    con.execute("""INSERT INTO updates
        (ts,ok,probe_ms,list_pages,new_items,changed,refreshed,reqs,elapsed,note)
        VALUES (?,1,?,?,?,?,?,?,?,?)""",
        (now, probe, pages, added, changed, len(stale), reqs[0], el,
         f"新增{added} 異動{changed}"))
    con.commit()
    log(f"完成：新增 {added} 筆，異動 {changed} 筆。"
        f"共 {reqs[0]} 次請求，{el:.1f} 秒")


if __name__ == "__main__":
    main()
