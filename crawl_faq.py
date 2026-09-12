#!/usr/bin/env python3
"""
臺北市政府全球資訊網「常見問答」爬蟲
https://www.gov.taipei/News.aspx?n=EEC70A4186D4C828&sms=87415A8B9CE81B16

兩階段、可中斷續跑，全部資料存進 faq.db (SQLite)。

  python3 crawl_faq.py list      # 階段一：抓清單（42 頁，約 1 分鐘）
  python3 crawl_faq.py detail    # 階段二：抓內文（8千多筆，可隨時 Ctrl-C，再跑會續傳）
  python3 crawl_faq.py stats     # 看進度
  python3 crawl_faq.py export    # 匯出 faq.csv / faq.json

速度控制在 RATE（每秒請求數）。預設 3.0，約 50 分鐘跑完。
想快一點改成 5.0（約 30 分鐘）；不建議超過 8。
"""

import argparse
import gzip
import html
import json
import os
import queue
import random
import re
import sqlite3
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Windows 的主控台預設是 cp950，印繁體中文會 UnicodeEncodeError。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


import faqlib as F

DB = F.DB
LIST_URL = F.LIST_URL + "&page={page}&PageSize={size}"
DETAIL_URL = F.BASE + "/News_Content.aspx?n=" + F.N + "&sms=" + F.SMS + "&s={sid}"

# ---- 速度／禮貌設定 ---------------------------------------------------------
RATE = 3.0          # 全域每秒請求數上限（不是每個 thread，是總量）
WORKERS = 1         # 併發連線數。實測限速之後開幾條都一樣快，用 1 最低調。
TIMEOUT = 30
MAX_RETRY = 3
ABORT_AFTER_BLOCKS = 5   # 連續被擋這麼多次就整個停下來，不硬衝

limiter = F.RateLimiter(1000.0 / RATE)
blocked_streak = threading.Semaphore(ABORT_AFTER_BLOCKS)
abort = threading.Event()

_local = threading.local()


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = F.new_session(pool=WORKERS + 1)
    return s


def fetch(url):
    """回傳 HTML 字串；被擋、拿到錯誤頁、或連續失敗都回 None。"""
    for attempt in range(MAX_RETRY):
        if abort.is_set():
            return None
        limiter.wait()
        try:
            r = session().get(url, timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(2 ** attempt + random.random())
            continue

        if r.status_code == 200:
            r.encoding = "utf-8"
            # 被擋時站方回 3 KB 的錯誤頁，狀態碼仍是 200。
            # 當作沒抓到，讓上層跳過，絕不寫進資料庫覆蓋既有內容。
            if "News_Content" in url and F.is_blocked(r):
                return None
            return r.text

        if r.status_code in (403, 429, 503):
            # 明確被限流：退讓，而不是重試更兇
            if not blocked_streak.acquire(blocking=False):
                sys.stderr.write(
                    f"\n!! 連續被擋 {ABORT_AFTER_BLOCKS} 次（HTTP {r.status_code}），停止。\n"
                    f"   等幾小時或隔天再跑，進度已存在 {DB}，會自動續傳。\n")
                abort.set()
                return None
            back = 30 * (attempt + 1) + random.uniform(0, 10)
            sys.stderr.write(f"\n   HTTP {r.status_code}，退讓 {back:.0f} 秒…\n")
            time.sleep(back)
            continue

        if r.status_code == 404:
            return None
        time.sleep(2 ** attempt)
    return None


parse_list = F.parse_list
roc_to_iso = F.roc_to_iso


def parse_detail(page_html):
    """回傳 (內文HTML, 內文純文字, 中繼資料dict) —— 保持舊介面"""
    d = F.parse_detail(page_html)
    info = {k: d[k] for k in
            ("title", "published", "updated", "reviewed", "expire",
             "maintainer", "dept")}
    return (d["body_html"] or None), (d["body"] or None), info


# ---- DB ---------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS faq (
        sid       TEXT PRIMARY KEY,
        no        INTEGER,
        title     TEXT,
        dept      TEXT,
        date      TEXT,
        url       TEXT,
        answer    TEXT,
        answer_html TEXT,
        kind      TEXT DEFAULT 'internal',
        fetched_at TEXT
    )""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(faq)")}
    for c, t in [("kind", "TEXT DEFAULT 'internal'"), ("published", "TEXT"),
                 ("updated", "TEXT"), ("published_iso", "TEXT")]:
        if c not in cols:
            con.execute(f"ALTER TABLE faq ADD COLUMN {c} {t}")
    con.commit()
    return con


# ---- 階段一：清單 -----------------------------------------------------------
def cmd_list(args):
    con = db()
    size = 200
    first = fetch(LIST_URL.format(page=1, size=size))
    if not first:
        sys.exit("清單第 1 頁抓不到，網站可能有異動。")
    total = int(TOTAL_RE.search(first).group(1))
    pages = (total + size - 1) // size
    print(f"共 {total} 筆，{pages} 頁（每頁 {size}）")

    new = 0
    seen_sids = set()
    for p in range(1, pages + 1):
        h = first if p == 1 else fetch(LIST_URL.format(page=p, size=size))
        if not h:
            print(f"  第 {p} 頁失敗，跳過")
            continue
        rows = parse_list(h)
        # 清單是活的，翻頁時同一筆可能出現在相鄰兩頁，先去重
        rows = [r for r in rows
                if not (r["sid"] in seen_sids or seen_sids.add(r["sid"]))]
        cur = con.executemany(
            """INSERT INTO faq (sid,no,title,dept,date,url,kind)
               VALUES (:sid,:no,:title,:dept,:date,:url,:kind)
               ON CONFLICT(sid) DO UPDATE SET
                 no=excluded.no, title=excluded.title,
                 dept=excluded.dept, date=excluded.date,
                 url=excluded.url, kind=excluded.kind""",
            rows)
        con.commit()
        new += len(rows)
        print(f"  第 {p:>3}/{pages} 頁  {len(rows)} 筆", flush=True)
        if abort.is_set():
            break

    n = con.execute("SELECT COUNT(*) FROM faq").fetchone()[0]
    print(f"清單完成，資料庫現有 {n} 筆（本次掃到 {new}）")


# ---- 階段二：內文 -----------------------------------------------------------
def cmd_detail(args):
    con = db()
    todo = con.execute(
        "SELECT sid, url FROM faq WHERE answer IS NULL AND kind='internal' "
        "ORDER BY no").fetchall()
    ext = con.execute(
        "SELECT COUNT(*) FROM faq WHERE kind='external'").fetchone()[0]
    if ext:
        print(f"（另有 {ext} 筆是連到外部網站的項目，只留清單與連結，不抓內文）")
    if not todo:
        print("沒有待抓的內文，全部完成。")
        return
    if args.limit:
        todo = todo[:args.limit]

    eta = len(todo) / RATE
    print(f"待抓 {len(todo)} 筆，速率 {RATE} req/s，"
          f"預估 {eta/60:.0f} 分鐘。Ctrl-C 可隨時中斷，進度會保留。")

    done = [0]
    fail = [0]
    lock = threading.Lock()
    writeq = queue.Queue()
    t0 = time.time()

    def worker(item):
        sid, url = item
        if abort.is_set():
            return
        h = fetch(url)
        if not h:
            with lock:
                fail[0] += 1
            return
        body, text, info = parse_detail(h)
        if not text:            # 解析不到內文就跳過，絕不覆蓋既有資料
            with lock:
                fail[0] += 1
            return
        pub = info["published"]
        writeq.put((text or "", body or "", info["title"], info["dept"],
                    pub, info["updated"], roc_to_iso(pub),
                    time.strftime("%Y-%m-%d %H:%M:%S"), sid))
        with lock:
            done[0] += 1
            n = done[0]
        if n % 25 == 0:
            el = time.time() - t0
            rate = n / el
            left = (len(todo) - n) / rate if rate else 0
            print(f"\r  {n}/{len(todo)}  {rate:.1f} req/s  "
                  f"剩 {left/60:.0f} 分  失敗 {fail[0]}   ", end="", flush=True)

    def writer():
        # sqlite 連線不能跨執行緒，寫入端自己開一條
        wcon = db()
        buf = []

        def flush():
            if not buf:
                return
            wcon.executemany(
                "UPDATE faq SET answer=?, answer_html=?, "
                "title=COALESCE(NULLIF(?,''),title), "
                "dept=COALESCE(NULLIF(?,''),dept), "
                "published=COALESCE(NULLIF(?,''),date), updated=?, "
                "published_iso=COALESCE(NULLIF(?,''),published_iso), "
                "fetched_at=? WHERE sid=?", buf)
            wcon.commit()
            buf.clear()

        while True:
            item = writeq.get()
            if item is None:
                break
            buf.append(item)
            if len(buf) >= 50:
                flush()
        flush()
        wcon.close()

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(worker, todo))
    except KeyboardInterrupt:
        abort.set()
        print("\n中斷中，正在存檔…")
    finally:
        writeq.put(None)
        wt.join()

    print(f"\n本次完成 {done[0]} 筆，失敗 {fail[0]} 筆，"
          f"耗時 {(time.time()-t0)/60:.1f} 分")
    cmd_stats(args)


# ---- 其他 -------------------------------------------------------------------
def cmd_stats(args):
    con = db()
    total, got, ext = con.execute(
        "SELECT COUNT(*), COUNT(answer), "
        "SUM(kind='external') FROM faq").fetchone()
    print(f"清單 {total} 筆（其中外部連結 {ext or 0} 筆）"
          f"，已抓內文 {got} 筆，待抓 {total - got - (ext or 0)} 筆")
    if total:
        print("\n各機關前 10：")
        for dept, c in con.execute(
                "SELECT dept, COUNT(*) c FROM faq GROUP BY dept "
                "ORDER BY c DESC LIMIT 10"):
            print(f"  {c:>5}  {dept}")


def cmd_export(args):
    import csv
    con = db()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT sid,no,title,dept,published,updated,kind,url,answer "
        "FROM faq ORDER BY published_iso DESC, no")]
    if not rows:
        sys.exit("資料庫是空的，先跑 list。")
    d = os.path.dirname(DB)
    with open(os.path.join(d, "faq.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(d, "faq.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"匯出 {len(rows)} 筆 → faq.csv / faq.json")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    d = sub.add_parser("detail")
    d.add_argument("--limit", type=int, help="只抓前 N 筆（測試用）")
    d.set_defaults(fn=cmd_detail)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
