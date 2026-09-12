#!/usr/bin/env python3
"""
臺北市政府常見問答 — 爬蟲工作台（本機網頁介面）

    python3 app.py          # 開 http://127.0.0.1:8765

可以選抓法、選筆數、選發布日期區間，跑完看耗時／流量，
歷次測試會累積成一張比較表，資料可匯出 CSV / JSON。
只用 Python 標準函式庫 + requests。
"""

import sys
import csv
import html as _html
import io
import json
import os
import platform
import random
import re
import socketserver
import sqlite3
import ssl
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

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

HERE, DB = F.HERE, F.DB
PORT = 8765
BASE, N, SMS = F.BASE, F.N, F.SMS
LIST_URL, POST_URL, COUNTER_URL = F.LIST_URL, F.POST_URL, F.COUNTER_URL
UA = F.UA

# ---- 抓法 -------------------------------------------------------------------
# per_request_session：每筆都開新連線（重新 TLS 握手）
# per_thread_session ：每條 thread 自己一條連線
# warm_session       ：先進清單頁拿 cookie，逐筆帶 Referer
# hit_counter        ：每筆再補一次點閱數回報（真瀏覽器會做）
METHODS = {
    "A": ("每筆重開連線（沒用 Session）",
          "最直覺也最慢：每一筆都重新握手一次 TLS。",
          dict(keep_alive=False, per_request_session=True)),
    "B": ("直接抓・循序",
          "一條連線重複使用，一筆一筆抓。",
          dict()),
    "C": ("直接抓・循序・關掉 gzip",
          "同 B 但不壓縮，用來看壓縮省了多少流量。",
          dict(gzip=False)),
    "D": ("完整瀏覽器流程（cookie + Referer + 點閱回報）",
          "先進清單頁拿 cookie，每筆帶 Referer，再補一次點閱回報。"
          "最像真人，但多一倍請求數。",
          dict(warm_session=True, hit_counter=True)),
    "E": ("直接抓・併發 3　★建議",
          "三條連線同時跑。速度與禮貌的平衡點。",
          dict(workers=3, per_thread_session=True)),
    "F": ("直接抓・併發 8",
          "八條連線。更快，但對伺服器不太客氣。",
          dict(workers=8, per_thread_session=True)),
}
DEFAULT_METHOD = "E"

# runs 表除了原本那幾欄之外，額外記錄的分析因素。
# 想加新因素只要往這裡加一列，舊資料庫會自動 ALTER。
RUN_COLS = [
    ("kept", "REAL DEFAULT 0"),        # 5 個欄位的位元數（這批總和）
    ("kept_min", "INTEGER DEFAULT 0"),  # 單筆最小 / 中位 / 最大
    ("kept_p50", "INTEGER DEFAULT 0"),
    ("kept_max", "INTEGER DEFAULT 0"),
    ("t_wait", "REAL DEFAULT 0"),      # 等伺服器總秒數（送出→收到 header）
    ("t_read", "REAL DEFAULT 0"),      # 收 body 總秒數
    ("t_parse", "REAL DEFAULT 0"),     # 解析 HTML 總秒數
    ("workers", "INTEGER DEFAULT 1"),  # 併發數
    ("keep_alive", "INTEGER DEFAULT 1"),
    ("gzip", "INTEGER DEFAULT 1"),
    ("warm_session", "INTEGER DEFAULT 0"),
    ("hit_counter", "INTEGER DEFAULT 0"),
    ("page_size", "INTEGER DEFAULT 0"),
    ("list_reqs", "INTEGER DEFAULT 0"),   # 清單階段用掉幾次請求
    ("detail_reqs", "INTEGER DEFAULT 0"), # 內文階段用掉幾次請求
    ("lat_min", "REAL DEFAULT 0"),        # 單次請求延遲分布（秒）
    ("lat_p50", "REAL DEFAULT 0"),
    ("lat_p95", "REAL DEFAULT 0"),
    ("lat_max", "REAL DEFAULT 0"),
    ("errors", "INTEGER DEFAULT 0"),
    ("host", "TEXT"),                     # 哪台機器跑的
    ("interval_ms", "REAL DEFAULT 0"),    # 限速間隔（0 = 不限速）
    ("blocked", "INTEGER DEFAULT 0"),     # 拿到錯誤頁（被擋）的次數
    ("retries", "INTEGER DEFAULT 0"),     # 網路逾時重試次數
]


# ---- 連線 -------------------------------------------------------------------
new_session = F.new_session
RateLimiter = F.RateLimiter


# ---- 解析（全部來自 faqlib，確保各腳本用的是同一份邏輯）--------------------
ANCHOR = F.ANCHOR
TOTAL_RE, HIDDEN_RE, QUERY_RE = F.TOTAL_RE, F.HIDDEN_RE, F.QUERY_RE
parse_list = F.parse_list
total_count = F.total_count
parse_detail = F.parse_detail
roc_to_iso = F.roc_to_iso
iso_to_roc_slash = F.iso_to_roc_slash
now_ts = F.now_ts
MIN_PAGE = F.MIN_PAGE


# ---- 資料庫 -----------------------------------------------------------------
def db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS faq (
        sid TEXT PRIMARY KEY, no INTEGER, title TEXT, dept TEXT, date TEXT,
        url TEXT, answer TEXT, answer_html TEXT, kind TEXT DEFAULT 'internal',
        fetched_at TEXT)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(faq)")}
    for c, t in [("kind", "TEXT DEFAULT 'internal'"), ("published", "TEXT"),
                 ("updated", "TEXT"), ("published_iso", "TEXT"),
                 ("bytes_needed", "INTEGER"), ("bytes_wire", "INTEGER"),
                 ("bytes_raw", "INTEGER"), ("reviewed", "TEXT"),
                 ("expire", "TEXT"), ("maintainer", "TEXT"),
                 ("last_listed", "TEXT"), ("files", "TEXT"),
                 ("file_count", "INTEGER DEFAULT 0")]:
        if c not in cols:
            con.execute(f"ALTER TABLE faq ADD COLUMN {c} {t}")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY, v TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, method TEXT,
        label TEXT, n INTEGER, done INTEGER, elapsed REAL, reqs INTEGER,
        wire INTEGER, raw INTEGER, stopped INTEGER DEFAULT 0, note TEXT)""")
    rcols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    for c, t in RUN_COLS:
        if c not in rcols:
            con.execute(f"ALTER TABLE runs ADD COLUMN {c} {t}")
    # 每次執行 × 每一筆各一列。faq 表會被後續執行覆蓋，這張不會，
    # 之後想到新的分析角度都能回頭算。
    con.execute("""CREATE TABLE IF NOT EXISTS samples (
        run_id INTEGER, seq INTEGER, sid TEXT, no INTEGER,
        method TEXT, workers INTEGER, gzip INTEGER, keep_alive INTEGER,
        warm_session INTEGER, hit_counter INTEGER,
        title TEXT, dept TEXT, published TEXT, updated TEXT,
        chars INTEGER, bytes_wire INTEGER, bytes_raw INTEGER,
        bytes_needed INTEGER,
        t_wait REAL, t_read REAL, t_total REAL,
        t_start REAL, t_end REAL, worker TEXT, ts TEXT,
        PRIMARY KEY (run_id, sid))""")
    scols = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
    for c in ("t_start", "t_end"):
        if c not in scols:
            con.execute(f"ALTER TABLE samples ADD COLUMN {c} REAL")
    if "worker" not in scols:
        con.execute("ALTER TABLE samples ADD COLUMN worker TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS ix_s_run ON samples(run_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_s_sid ON samples(sid)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_pub ON faq(published_iso)")
    con.commit()
    return con


# ---- 抓取工作 ---------------------------------------------------------------
JOB = {"running": False, "phase": "", "done": 0, "total": 0, "blocked": 0,
       "elapsed": 0.0,
       "eta": 0.0, "wire": 0, "raw": 0, "reqs": 0, "method": "", "log": "",
       "error": "", "finished_at": "", "stopped": False, "kept": 0}
LOCK = threading.Lock()
ABORT = threading.Event()


def meta_get(con, k, default=""):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, **kv):
    con.executemany("INSERT INTO meta (k,v) VALUES (?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    [(k, str(v)) for k, v in kv.items()])


# 清單階段的寫入。抓整站和清單對帳共用同一份，兩邊的欄位不會走鐘。
LIST_UPSERT = """
    INSERT INTO faq (sid,no,title,dept,date,url,kind,
                     published,published_iso,last_listed)
    VALUES (:sid,:no,:title,:dept,:date,:url,:kind,
            :published,:published_iso,:last_listed)
    ON CONFLICT(sid) DO UPDATE SET
      no=excluded.no, url=excluded.url, kind=excluded.kind,
      last_listed=excluded.last_listed"""


def save_list(con, items, ts):
    con.executemany(LIST_UPSERT,
        [{"sid": x["sid"], "no": x["no"], "title": x["list_title"],
          "dept": x["list_dept"], "date": x["list_date"],
          "url": x["url"], "kind": x["kind"],
          "published": x["list_date"],
          "published_iso": roc_to_iso(x["list_date"]),
          "last_listed": ts} for x in items])


def jset(**kv):
    with LOCK:
        JOB.update(kv)


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def crawl(method, n, d_from="", d_to="", interval_ms=0):
    label, _, kw = METHODS[method]
    keep_alive = kw.get("keep_alive", True)
    gz = kw.get("gzip", True)
    workers = kw.get("workers", 1)
    warm = kw.get("warm_session", False)
    counter = kw.get("hit_counter", False)
    per_req = kw.get("per_request_session", False)
    per_thr = kw.get("per_thread_session", False)

    limiter = RateLimiter(interval_ms)
    st = {"wire": 0, "raw": 0, "reqs": 0, "done": 0, "errors": 0,
          "blocked": 0, "streak": 0, "retries": 0,
          "t_wait": 0.0, "t_read": 0.0, "t_parse": 0.0, "lat": []}
    MAX_BLOCK_STREAK = 30   # 連續這麼多次拿到錯誤頁就整個停下來
    lock = threading.Lock()
    tl = threading.local()
    t0 = time.perf_counter()

    def timed(fn, *a, **k):
        """把一次請求拆成兩段計時：
           等伺服器（送出 → 收到 header，含連線建立與伺服器產頁）
           下載內文（收 header → 收完 body）

        網路逾時／斷線會重試 —— 以前沒重試，一個 timeout 就讓整輪報銷。
        """
        import requests as _rq
        last = None
        for attempt in range(3):
            if ABORT.is_set():
                raise RuntimeError("已中斷")
            limiter.wait()
            t0 = time.perf_counter()
            try:
                r = fn(*a, stream=True, **k)
                t1 = time.perf_counter()
                body = r.content
                t2 = time.perf_counter()
            except (_rq.Timeout, _rq.ConnectionError, _rq.ChunkedEncodingError) as e:
                last = e
                with lock:
                    st["retries"] = st.get("retries", 0) + 1
                setj(log=f"連線問題（{type(e).__name__}），"
                         f"{2 ** attempt} 秒後重試…")
                time.sleep(2 ** attempt + random.random())
                continue
            r.encoding = "utf-8"
            with lock:
                st["wire"] += int(r.headers.get("Content-Length") or len(body))
                st["raw"] += len(body)
                st["reqs"] += 1
                st["t_wait"] += t1 - t0
                st["t_read"] += t2 - t1
                st["lat"].append(t2 - t0)
            r._wire = int(r.headers.get("Content-Length") or len(body))
            r._raw = len(body)
            r._t_wait = t1 - t0
            r._t_read = t2 - t1
            r._t_abs = t0
            return r
        raise last

    shared = new_session(keep_alive, gz)

    def sess():
        if per_req:
            return new_session(keep_alive, gz)
        if per_thr:
            s = getattr(tl, "s", None)
            if s is None:
                s = tl.s = new_session(keep_alive, gz)
            return s
        return shared

    def setj(**kv):
        with LOCK:
            JOB.update(kv)

    # --- 階段一：清單 -------------------------------------------------------
    # 有指定日期就走網站的查詢表單（POST + __VIEWSTATE），拿到一個 _Query
    # token；那個 token 是無狀態的，之後翻頁可以直接 GET。
    token = ""
    if d_from or d_to:
        setj(phase="查詢", log="送出日期查詢…")
        r = timed(shared.get, LIST_URL, timeout=30)
        form = {k: _html.unescape(v) for k, v in HIDDEN_RE.findall(r.text)}
        form.update({
            "jNewsModule_field_SDate_1": iso_to_roc_slash(d_from),
            "jNewsModule_field_EDate_1": iso_to_roc_slash(d_to),
            "jNewsModule_BtnSend": "送出查詢",
        })
        p = timed(shared.post, POST_URL, data=form, timeout=30)
        q = QUERY_RE.search(p.text)
        if not q:
            raise RuntimeError("網站沒有回傳查詢結果（日期格式可能不對）")
        token = q.group(1)
        hits = TOTAL_RE.search(p.text)
        setj(log=f"日期查詢命中 {hits.group(1) if hits else '?'} 筆")

    setj(phase="清單", log=JOB.get("log") or "抓清單…")
    items, seen, page, dup = [], set(), 1, 0
    site_total = 0                  # 站方分頁列自己標的總筆數，拿來對帳
    size = min(200, max(10, n))
    while len(items) < n and not ABORT.is_set():
        u = f"{LIST_URL}&page={page}&PageSize={size}"
        if token:
            u += f"&_Query={token}"
        try:
            r = timed(shared.get, u, timeout=45)
        except Exception as e:
            setj(log=f"清單第 {page} 頁失敗（{type(e).__name__}），跳過")
            page += 1
            if page > 60:
                break
            continue
        rows = parse_list(r.text)
        if not site_total:
            site_total = total_count(r.text) or 0
        if not rows:
            break
        # 站上的清單是活的，翻頁時資料會位移，同一筆可能同時出現在
        # 第 N 頁末和第 N+1 頁頭。不去重的話會重複抓、筆數也會灌水。
        added = 0
        for x in rows:
            if x["sid"] in seen:
                dup += 1
                continue
            seen.add(x["sid"])
            items.append(x)
            added += 1
        # 站方對超出範圍的頁碼不回空值，而是一直回最後一頁。
        # 所以「整頁都是看過的」才是真正的結束訊號，不能只靠 rows 是空的。
        if added == 0:
            break
        page += 1
    if dup:
        setj(log=f"清單去重：跳過 {dup} 筆重複")

    # 清單階段就把每一筆寫進資料庫（含連到外部網站、沒有站內內文的那些），
    # 內文階段再補內容。這樣資料庫的筆數才會跟站上一致 ——
    # 只在「抓到內文」時才寫的話，外部連結永遠不會進來。
    if items:
        ts = now_ts()
        lcon = db()
        save_list(lcon, items, ts)
        # 只有「這一趟真的把整站清單翻完」才更新對帳基準，
        # 否則抓 100 筆的測試會把基準蓋掉，匯出就只剩 100 筆。
        if site_total and len(items) >= site_total:
            meta_set(lcon, last_full_list=ts, site_total=site_total,
                     listed=len(items))
        lcon.commit()
        lcon.close()

    items = [i for i in items if i["kind"] == "internal"][:n]
    list_reqs = st["reqs"]
    setj(phase="內文", total=len(items),
         log=f"清單 {len(items)} 筆"
             + (f"（已去除 {dup} 筆重複）" if dup else "")
             + "，開始逐筆進去抓內容…")

    # --- 階段二：逐筆抓內文 -------------------------------------------------
    def one(it):
        if ABORT.is_set():
            return None
        s = sess()
        hdr = {"Referer": LIST_URL} if warm else {}
        try:
            r = timed(s.get, it["url"], headers=hdr, timeout=30)
        except Exception:
            with lock:
                st["errors"] += 1
            return None

        # 擋掉錯誤頁（判斷邏輯在 faqlib.is_blocked，各腳本共用一份）：
        # 這次沒拿到就跳過，絕不寫進資料庫覆蓋既有內容。
        if F.is_blocked(r):
            with lock:
                st["blocked"] += 1
                st["streak"] += 1
                streak = st["streak"]
            if streak >= MAX_BLOCK_STREAK:
                ABORT.set()
                setj(error=f"連續 {streak} 次拿到錯誤頁（伺服器正在擋），"
                           f"已停止。既有資料未被覆蓋。")
            return None
        with lock:
            st["streak"] = 0
        tp = time.perf_counter()
        d = parse_detail(r.text)
        with lock:
            st["t_parse"] += time.perf_counter() - tp
        d["bytes_wire"] = r._wire      # 這一頁實際下載了多少（gzip 後）
        d["bytes_raw"] = r._raw        # 伺服器產出的完整 HTML
        d["t_wait"] = r._t_wait
        d["t_read"] = r._t_read
        d["t_start"] = r._t_abs - t0        # 請求送出的時刻（不含限速等待）
        d["t_end"] = time.perf_counter() - t0
        d["worker"] = threading.current_thread().name
        if counter:
            c = timed(s.post, COUNTER_URL,
                      data={"n": N, "s": it["sid"], "smlsn": SMS},
                      headers={"Referer": it["url"]})
            d["bytes_wire"] += c._wire      # 點閱回報也算這一筆的成本
            d["bytes_raw"] += c._raw
            d["t_wait"] += c._t_wait
            d["t_read"] += c._t_read
            d["t_end"] = time.perf_counter() - t0
        if per_req:
            s.close()
        with lock:
            st["done"] += 1
            done = st["done"]
        el = time.perf_counter() - t0
        setj(done=done, elapsed=el, blocked=st["blocked"],
             eta=(len(items) - done) * el / done if done else 0.0)
        return dict(it, **d)

    if workers == 1:
        rows = [one(i) for i in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(one, items))
    rows = [r for r in rows if r]
    elapsed = time.perf_counter() - t0

    # --- 存檔 ---------------------------------------------------------------
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    recs = []
    for r in rows:
        pub = r["published"] or r["list_date"]
        rec = {
            "sid": r["sid"], "no": r["no"], "url": r["url"], "kind": r["kind"],
            "title": r["title"] or r["list_title"],
            "dept": r["dept"] or r["list_dept"],
            "date": r["list_date"], "published": pub, "updated": r["updated"],
            "published_iso": roc_to_iso(pub), "answer": r["body"],
            # 原始 HTML 一併保留：超連結、換行、粗體、表格在 clean_text()
            # 之後就沒了，官網長什麼樣要靠這一份才還原得回來。
            "answer_html": r.get("body_html") or "",
            "reviewed": r.get("reviewed", ""), "expire": r.get("expire", ""),
            "maintainer": r.get("maintainer", ""),
            "files": json.dumps(r.get("files") or [], ensure_ascii=False),
            "file_count": len(r.get("files") or []),
            "fetched_at": now,
            "bytes_wire": r.get("bytes_wire", 0),
            "bytes_raw": r.get("bytes_raw", 0),
        }
        rec["bytes_needed"] = needed_bytes(rec)
        recs.append(rec)
    con = db()
    con.executemany("""
        INSERT INTO faq (sid,no,title,dept,date,url,kind,answer,answer_html,
                         published,updated,published_iso,fetched_at,
                         bytes_needed,bytes_wire,bytes_raw,
                         reviewed,expire,maintainer,files,file_count)
        VALUES (:sid,:no,:title,:dept,:date,:url,:kind,:answer,:answer_html,
                :published,:updated,:published_iso,:fetched_at,
                :bytes_needed,:bytes_wire,:bytes_raw,
                :reviewed,:expire,:maintainer,:files,:file_count)
        ON CONFLICT(sid) DO UPDATE SET
          no=excluded.no, title=excluded.title, dept=excluded.dept,
          date=excluded.date, answer=excluded.answer,
          answer_html=excluded.answer_html,
          published=excluded.published, updated=excluded.updated,
          published_iso=excluded.published_iso,
          fetched_at=excluded.fetched_at,
          bytes_needed=excluded.bytes_needed,
          bytes_wire=excluded.bytes_wire,
          bytes_raw=excluded.bytes_raw,
          reviewed=excluded.reviewed, expire=excluded.expire,
          maintainer=excluded.maintainer,
          files=excluded.files, file_count=excluded.file_count
        -- 用「有沒有標題」判斷是不是真的抓到：錯誤頁沒有 og:title。
        -- 不能用「有沒有內文」—— 有些問答的答案是 PDF 附件，
        -- 也有些站上本來就只建了標題沒填內容，那些都不是失敗。
        WHERE excluded.title IS NOT NULL AND excluded.title <> ''""", recs)
    needs = sorted(r["bytes_needed"] for r in recs) or [0]
    kept = sum(needs)
    stopped = ABORT.is_set()
    note = f"{d_from or '不限'}~{d_to or '不限'}" if (d_from or d_to) else "全部"
    if interval_ms:
        note += f"　限速{interval_ms:.0f}ms"
    lat = sorted(st["lat"])

    def pct(p):
        return lat[min(int(len(lat) * p), len(lat) - 1)] if lat else 0.0

    fields = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "method": method,
        "label": label, "n": n, "done": len(rows), "elapsed": elapsed,
        "reqs": st["reqs"], "wire": st["wire"], "raw": st["raw"],
        "stopped": int(stopped), "note": note, "kept": kept,
        "t_wait": st["t_wait"], "t_read": st["t_read"],
        "kept_min": needs[0], "kept_max": needs[-1],
        "kept_p50": needs[len(needs) // 2],
        "t_parse": st["t_parse"], "workers": workers,
        "keep_alive": int(keep_alive), "gzip": int(gz),
        "warm_session": int(warm), "hit_counter": int(counter),
        "page_size": size, "list_reqs": list_reqs,
        "detail_reqs": st["reqs"] - list_reqs, "errors": st["errors"],
        "lat_min": lat[0] if lat else 0.0, "lat_p50": pct(.50),
        "lat_p95": pct(.95), "lat_max": lat[-1] if lat else 0.0,
        "host": platform.node(), "interval_ms": interval_ms,
        "blocked": st["blocked"], "retries": st.get("retries", 0),
    }
    cols = ",".join(fields)
    cur = con.execute(f"INSERT INTO runs ({cols}) "
                      f"VALUES ({','.join('?' * len(fields))})",
                      list(fields.values()))
    run_id = cur.lastrowid

    con.executemany("""INSERT OR REPLACE INTO samples
        (run_id,seq,sid,no,method,workers,gzip,keep_alive,warm_session,
         hit_counter,title,dept,published,updated,chars,
         bytes_wire,bytes_raw,bytes_needed,t_wait,t_read,t_total,
         t_start,t_end,worker,ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(run_id, i + 1, r["sid"], r["no"], method, workers, int(gz),
          int(keep_alive), int(warm), int(counter), rec["title"],
          rec["dept"], rec["published"], rec["updated"],
          len(rec["answer"] or ""), rec["bytes_wire"], rec["bytes_raw"],
          rec["bytes_needed"], r.get("t_wait", 0.0), r.get("t_read", 0.0),
          r.get("t_wait", 0.0) + r.get("t_read", 0.0),
          r.get("t_start", 0.0), r.get("t_end", 0.0),
          r.get("worker", ""), now)
         for i, (r, rec) in enumerate(zip(rows, recs))])
    con.commit()
    export_run(con, run_id)
    con.close()

    setj(running=False, phase="", elapsed=elapsed, eta=0.0, wire=st["wire"],
         raw=st["raw"], reqs=st["reqs"], done=len(rows), kept=kept,
         t_wait=st["t_wait"], t_read=st["t_read"],
         stopped=stopped, method=f"{method}　{label}",
         finished_at=time.strftime("%H:%M:%S"),
         log=(f"中斷，已存 {len(rows)} 筆" if stopped else f"完成 {len(rows)} 筆")
             + (f"（另有 {st['blocked']} 次被擋，已跳過未寫入）"
                if st["blocked"] else ""))


def reconcile_list(interval_ms=333.0):
    """只翻清單、不抓內文，把站上現有的每一筆蓋上同一個時間戳。
    匯出以這個時間戳為準，筆數就跟官網對得起來。

    頁數用站方標的總筆數算（faqlib.total_count），不靠「翻到沒東西為止」——
    站方對超出範圍的頁碼會一直回最後一頁。翻頁時清單是活的、會漂移，
    一輪偶爾漏一兩筆，所以收不齊就再翻一輪，最多三輪。
    """
    s = new_session()
    limiter = RateLimiter(interval_ms)
    seen, site_total, rounds, settled = {}, 0, 0, False
    t0 = time.perf_counter()
    for rnd in (1, 2, 3):
        rounds = rnd
        before = len(seen)
        page, last_page = 1, None
        while (last_page is None or page <= last_page) and not ABORT.is_set():
            limiter.wait()
            try:
                r = s.get(f"{LIST_URL}&page={page}&PageSize={F.MAX_PAGE_SIZE}",
                          timeout=45)
            except Exception as e:
                jset(log=f"第 {page} 頁失敗（{type(e).__name__}），跳過")
                page += 1
                if last_page is None and page > 60:
                    break
                continue
            if not site_total:
                site_total = total_count(r.text) or 0
            if last_page is None:
                last_page = (-(-site_total // F.MAX_PAGE_SIZE)) if site_total else 60
            for x in parse_list(r.text):
                seen.setdefault(x["sid"], x)
            jset(done=len(seen), total=site_total,
                 log=f"第 {rnd} 輪　第 {page}/{last_page} 頁　已收 {len(seen)} 筆")
            page += 1
        if ABORT.is_set():
            break
        # 站方 pager 標的數字自己沒去重，會比實際多一兩筆。追著它跑會
        # 白翻好幾輪，所以「整整一輪都沒收到新東西」才是收齊的訊號。
        if site_total and len(seen) >= site_total:
            settled = True
            break
        if rnd > 1 and len(seen) == before:
            settled = True
            break

    items = list(seen.values())
    ts = now_ts()
    con = db()
    if items and not ABORT.is_set():
        save_list(con, items, ts)
        meta_set(con, last_full_list=ts, site_total=site_total,
                 listed=len(items), settled=int(settled))
        con.commit()
    delisted = con.execute(
        "SELECT COUNT(*) FROM faq WHERE last_listed IS NULL "
        "OR last_listed <> ?", (ts,)).fetchone()[0]
    con.close()
    el = time.perf_counter() - t0
    gap = site_total - len(items)
    jset(running=False, phase="", done=len(items), total=site_total,
         finished_at=time.strftime("%H:%M:%S"),
         stopped=ABORT.is_set(),
         log=("已停止，基準沒有更新" if ABORT.is_set() else
              f"對帳完成：官網標 {site_total} 筆，實收 {len(items)} 筆"
              + ("　✓ 已收齊" if settled else f"　⚠ 可能還有漏（差 {gap} 筆）")
              + (f"（官網計數多 {gap} 筆，站方自己沒去重）"
                 if settled and gap > 0 else "")
              + f"　另有 {delisted} 筆已不在清單上（留在資料庫不刪）"
              + f"　翻 {rounds} 輪 / {el:.0f} 秒"))


def start_reconcile(interval_ms=333.0):
    with LOCK:
        if JOB["running"]:
            return False
        ABORT.clear()
        JOB.update(running=True, phase="清單對帳", done=0, total=0,
                   elapsed=0.0, eta=0.0, wire=0, raw=0, reqs=0, kept=0,
                   error="", finished_at="", stopped=False,
                   method="清單對帳（只翻清單，不抓內文）", log="開始…")

    def go():
        try:
            reconcile_list(interval_ms)
        except Exception as e:
            with LOCK:
                JOB.update(running=False, phase="",
                           error=f"{type(e).__name__}: {e}")

    threading.Thread(target=go, daemon=True).start()
    return True


def start_job(method, n, d_from="", d_to="", interval_ms=0):
    with LOCK:
        if JOB["running"]:
            return False
        ABORT.clear()
        JOB.update(running=True, phase="準備", done=0, total=n, elapsed=0.0,
                   eta=0.0, wire=0, raw=0, reqs=0, kept=0, error="",
                   finished_at="", stopped=False,
                   method=f"{method}　{METHODS[method][0]}", log="開始…")

    def go():
        try:
            crawl(method, n, d_from, d_to, interval_ms)
        except Exception as e:
            with LOCK:
                JOB.update(running=False, phase="",
                           error=f"{type(e).__name__}: {e}")

    threading.Thread(target=go, daemon=True).start()
    return True


EXPORT_DIR = os.path.join(HERE, "exports")

SAMPLE_COLS = [
    ("seq", "序"), ("sid", "sid"), ("no", "站上編號"), ("method", "抓法"),
    ("workers", "併發數"), ("gzip", "gzip"), ("keep_alive", "keep_alive"),
    ("warm_session", "帶cookie"), ("hit_counter", "點閱回報"),
    ("published", "發布時間"), ("updated", "更新時間"), ("title", "標題"),
    ("dept", "發布機關"), ("chars", "內容字數"),
    ("bytes_wire", "抓下位元數"), ("bytes_raw", "網頁原始位元數"),
    ("bytes_needed", "需要位元數"),
    ("t_wait", "等伺服器ms"), ("t_read", "傳輸ms"), ("t_total", "單次總ms"),
    ("t_start", "起算秒"), ("t_end", "完成秒"), ("worker", "執行緒"),
    ("ts", "時間"),
]

# 秒 → 毫秒（取一位小數）；純秒數欄位取兩位。原始值都還在資料庫裡。
_MS = {"t_wait", "t_read", "t_total"}
_SEC = {"t_start", "t_end"}


def cell(en, v):
    if v is None:
        return ""
    if en in _MS:
        return round(v * 1000, 1)
    if en in _SEC:
        return round(v, 2)
    return v


def export_run(con, run_id):
    """把這次執行的逐筆原始測量寫成一個 CSV，檔名帶日期時間與抓法。"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM samples WHERE run_id=? ORDER BY seq", (run_id,))]
    con.row_factory = None
    if not rows:
        return ""
    m = rows[0]["method"]
    name = (f"{time.strftime('%Y%m%d_%H%M%S')}_方法{m}_{len(rows)}筆.csv")
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([zh for _, zh in SAMPLE_COLS])
        for r in rows:
            w.writerow([cell(en, r[en]) for en, _ in SAMPLE_COLS])
    return path


# ---- 查詢 / 匯出 ------------------------------------------------------------
FIELDS = F.FIELDS          # 欄位定義集中在 faqlib
needed_bytes = F.needed_bytes

# openpyxl 不收 XML 規格禁止的控制字元，內容裡偶爾會混到。
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xl_safe(v):
    return _ILLEGAL.sub("", v) if isinstance(v, str) else v


EXPORT_EXTRA = [("file_names", "附件"), ("file_count", "附件數"),
                ("bytes_wire", "抓下位元數"), ("bytes_raw", "網頁原始位元數"),
                ("bytes_needed", "需要位元數"), ("url", "網址")]


def with_files(rows):
    """把 files 的 JSON 展開成「檔名(類型, 大小)」一行一個，方便匯出。"""
    for r in rows:
        try:
            fs = json.loads(r.get("files") or "[]")
        except Exception:
            fs = []
        r["file_names"] = "\n".join(
            f"{f['name']}（{f['kind']} {f['size']}）".replace("（ ）", "")
            for f in fs)
    return rows


def needed_bytes(rec):
    """這一筆真正要留下來的位元數 —— 就是那 5 個欄位的 UTF-8 長度總和。
    中文一個字 3 bytes，所以內容長短會直接反映在這個數字上。"""
    return sum(len((rec.get(k) or "").encode("utf-8")) for k, _ in FIELDS)


def query(d1="", d2="", q="", limit=None):
    con = db()
    con.row_factory = sqlite3.Row
    sql = ("SELECT published,updated,reviewed,expire,title,dept,maintainer,"
           "answer,files,file_count,url,published_iso,bytes_needed,"
           "bytes_wire,bytes_raw FROM faq WHERE 1=1")
    args = []
    # 以最近一次完整清單掃描為準：出的就是「現在官網上有的那些」，
    # 筆數跟官網對得起來。已下架的仍留在資料庫，只是不列入。
    # 不再用 kind / answer 過濾 —— 外部連結和站上本來就空白的那幾筆
    # 也是官網真實存在的項目，藏起來筆數就對不上了。
    stamp = meta_get(con, "last_full_list")
    if stamp:
        sql += " AND last_listed = ?"
        args.append(stamp)
    if d1:
        sql += " AND published_iso >= ?"
        args.append(d1)
    if d2:
        sql += " AND published_iso <= ?"
        args.append(d2)
    if q:
        sql += " AND (title LIKE ? OR answer LIKE ? OR dept LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY published_iso DESC, no"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = with_files([dict(r) for r in con.execute(sql, args)])
    con.close()
    return rows


def overview():
    con = db()
    tot, got = con.execute(
        "SELECT COUNT(*), SUM(answer <> '' OR file_count > 0) FROM faq"
    ).fetchone()
    got = got or 0
    stamp = meta_get(con, "last_full_list")
    site_total = int(meta_get(con, "site_total", "0") or 0)
    settled = meta_get(con, "settled", "0") == "1"
    listed = con.execute("SELECT COUNT(*) FROM faq WHERE last_listed=?",
                         (stamp,)).fetchone()[0] if stamp else 0
    lo, hi = con.execute(
        "SELECT MIN(NULLIF(published_iso,'')), MAX(published_iso) "
        "FROM faq").fetchone()
    if not lo:   # 還沒抓過內文，就用清單日期推
        ds = [roc_to_iso(d) for (d,) in con.execute(
            "SELECT date FROM faq WHERE date <> ''")]
        ds = sorted(x for x in ds if x)
        lo, hi = (ds[0], ds[-1]) if ds else ("", "")
    con.row_factory = sqlite3.Row
    runs = [dict(r) for r in con.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 60")]
    con.row_factory = None
    con.close()
    return {"total": tot, "with_body": got, "lo": lo or "", "hi": hi or "",
            "stamp": stamp, "site_total": site_total, "listed": listed,
            "settled": settled,
            "delisted": (tot - listed) if stamp else 0, "runs": runs}


# ---- 工具（把命令列腳本變成網頁按鈕）--------------------------------------
TOOLS = {
    "check": ("環境檢查", [sys.executable, "check_env.py"],
              "檢查 Python、套件、網路、TLS、對外 IP，"
              "以及市府網站現在會不會擋我們"),
    "update": ("每日更新", [sys.executable, "update.py"],
               "只抓新增的問答，約十幾次請求、幾秒完成。平常用這個就好"),
    "verify": ("完整性驗證", [sys.executable, "verify.py",
                          "--sample", "25", "--longest"],
               "確認存下來的內文沒有被截斷（會重抓最長的 25 筆逐字比對）"),
    "report": ("產生分析報表", [sys.executable, "report.py"],
               "把歷次抓取的測量資料整理成 report.md"),
    "excel": ("產生分析 Excel", [sys.executable, "to_excel.py"],
              "含圖表的分析檔，跟資料匯出的 Excel 不同"),
}

TOOL = {"running": "", "name": "", "out": "", "rc": None,
        "started": "", "finished": ""}
TOOL_LOCK = threading.Lock()


def run_tool(key):
    label, cmd, _ = TOOLS[key]
    with TOOL_LOCK:
        TOOL.update(running=key, name=label, out="", rc=None,
                    started=time.strftime("%H:%M:%S"), finished="")

    def go():
        import subprocess
        try:
            p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace",
                                 bufsize=1)
            for line in p.stdout:
                with TOOL_LOCK:
                    TOOL["out"] += line
            p.wait()
            rc = p.returncode
        except Exception as e:
            with TOOL_LOCK:
                TOOL["out"] += f"\n執行失敗：{type(e).__name__}: {e}\n"
            rc = -1
        with TOOL_LOCK:
            TOOL.update(running="", rc=rc,
                        finished=time.strftime("%H:%M:%S"))

    threading.Thread(target=go, daemon=True).start()


# ---- 網頁 -------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="zh-TW"><meta charset="utf-8">
<title>常見問答爬蟲工作台</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f5f6f8;--card:#fff;--line:#e4e7eb;--ink:#1a1e24;--dim:#6b7280;
      --accent:#0cb5b5;--danger:#c0392b}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;
     background:var(--bg);color:var(--ink)}
header{background:var(--card);border-bottom:1px solid var(--line);padding:13px 22px;
       display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h1{margin:0;font-size:16px}
header .meta{color:var(--dim);font-size:12.5px}
main{max-width:1200px;margin:18px auto;padding:0 16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h2{margin:0 0 13px;font-size:13px;color:var(--dim);font-weight:600;
         letter-spacing:.05em}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:4px}
select,input,button{font:inherit;padding:7px 10px;border:1px solid var(--line);
                    border-radius:7px;background:#fff;color:var(--ink)}
select{min-width:340px}
button{background:var(--accent);color:#fff;border-color:transparent;cursor:pointer;
       font-weight:600;padding:8px 18px}
button:hover{filter:brightness(.93)}
button.ghost{background:#fff;color:var(--ink);border-color:var(--line);font-weight:400}
button.stop{background:var(--danger)}
button.warn{color:var(--danger);border-color:#f0c8c2}
button.warn:hover{background:#fdf0ee}
.danger{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:14px;
        padding:10px 13px;border:1px solid #f0c8c2;border-radius:8px;
        background:#fdf7f6;font-size:12.5px}
.danger b{color:var(--danger)}
.danger span{color:var(--dim)}
code{background:#fff;border:1px solid var(--line);border-radius:4px;
     padding:1px 5px;font-size:11.5px}
button:disabled{opacity:.45;cursor:default}
.hint{color:var(--dim);font-size:12.5px;margin-top:9px;min-height:1.5em}
.hint.err{color:var(--danger)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
       gap:1px;background:var(--line);border:1px solid var(--line);
       border-radius:8px;overflow:hidden;margin-top:13px}
.stat{background:var(--card);padding:10px 13px}
.stat b{display:block;font-size:19px;font-variant-numeric:tabular-nums;
        font-weight:600;line-height:1.3}
.stat span{font-size:11.5px;color:var(--dim)}
pre#toolout{background:#1b1f24;color:#d7dde3;padding:13px 15px;border-radius:8px;
   font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
   max-height:340px;overflow:auto;white-space:pre-wrap;margin:12px 0 0}
progress{width:100%;height:7px;margin-top:12px;accent-color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
      vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:11.5px;position:sticky;top:0;
   background:var(--card);z-index:1}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.sep{border-left:1px solid var(--line)}
td.t{max-width:320px}td.b{max-width:400px;color:#3c434c}
.wrap{max-height:500px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.badge{background:#eaf6f6;color:#0a7f7f;border-radius:99px;padding:2px 9px;
       font-size:11.5px}
a{color:#0a7f7f}
</style>
<header>
  <h1>臺北市政府常見問答 · 爬蟲工作台</h1>
  <span class="meta" id="ov">載入中…</span>
</header>
<main>

<div class="card">
  <h2>抓取</h2>
  <div class="row">
    <div><label>抓法</label><select id="m"></select></div>
    <div><label>筆數</label><input id="n" type="number" value="100" min="1"
         max="8400" style="width:96px"></div>
    <div><label>發布日期 從（可留空）</label><input id="cf" type="date"></div>
    <div><label>到</label><input id="ct" type="date"></div>
    <div><label>限速（每筆間隔）</label>
      <select id="iv" style="min-width:190px">
        <option value="0">不限速（測速度用）</option>
        <option value="100">100 ms　≈ 10 req/s</option>
        <option value="200">200 ms　≈ 5 req/s</option>
        <option value="333" selected>333 ms　≈ 3 req/s　★建議</option>
        <option value="500">500 ms　≈ 2 req/s</option>
        <option value="1000">1000 ms　≈ 1 req/s</option>
        <option value="-1">自訂…</option>
      </select></div>
    <div id="ivwrap" hidden><label>自訂 ms</label>
      <input id="ivc" type="number" value="333" min="0" max="10000"
             style="width:96px"></div>
    <button id="go">開始抓取</button>
    <button id="recon" class="ghost">清單對帳</button>
    <button id="stop" class="ghost stop" disabled>停止</button>
  </div>
  <div class="hint" id="desc"></div>
  <progress id="pg" value="0" max="100" hidden></progress>
  <div class="stats" id="stats" hidden>
    <div class="stat"><b id="s_time">–</b><span>總耗時</span></div>
    <div class="stat"><b id="s_each">–</b><span>平均每筆</span></div>
    <div class="stat"><b id="s_rps">–</b><span>請求／秒</span></div>
    <div class="stat"><b id="s_wire">–</b><span>實際下載</span></div>
    <div class="stat"><b id="s_raw">–</b><span>網頁原始量</span></div>
    <div class="stat"><b id="s_keep">–</b><span>留下的欄位</span></div>
    <div class="stat"><b id="s_wait">–</b><span>每筆等伺服器</span></div>
    <div class="stat"><b id="s_read">–</b><span>每筆傳輸</span></div>
  </div>
  <div class="hint" id="msg"></div>
</div>

<div class="card">
  <h2>工具　<span style="font-weight:400;letter-spacing:0">
    不用開命令列，按鈕就能跑</span></h2>
  <div class="row" id="toolbtns"></div>
  <div class="hint" id="tooldesc"></div>
  <pre id="toolout" hidden></pre>
</div>

<div class="card">
  <h2>歷次測試　<span style="font-weight:400;letter-spacing:0">
    同樣筆數換不同抓法跑，這裡會累積成比較表</span></h2>
  <div class="wrap" style="max-height:300px">
    <table><thead><tr>
      <th>時間</th><th>抓法</th><th class="num">限速</th><th>範圍</th>
      <th class="num">筆數</th><th class="num">耗時</th><th class="num">每筆</th>
      <th class="num sep">等伺服器</th><th class="num">傳輸</th>
      <th class="num sep">抓下/筆<br>
        <span style="font-weight:400;color:#9aa2ac">實際下載</span></th>
      <th class="num">原始HTML/筆<br>
        <span style="font-weight:400;color:#9aa2ac">解壓後</span></th>
      <th class="num">壓縮率</th>
      <th class="num sep">實際需要/筆<br>
        <span style="font-weight:400;color:#9aa2ac">平均・最小~最大</span></th>
      <th class="num">有效率</th><th></th>
    </tr></thead><tbody id="rt"></tbody></table>
  </div>
  <div class="row" style="margin-top:10px">
    <button class="ghost" id="runcsv">匯出測試紀錄 CSV</button>
    <button class="ghost" id="smpcsv">匯出逐筆原始資料 CSV</button>
    <button class="ghost" id="delruns">清空紀錄</button>
    <span style="flex:1"></span>
    <span style="color:#6b7280;font-size:12.5px">
      每次執行也會自動存一份逐筆原始資料到 exports/ 資料夾，檔名帶日期時間與抓法</span>
  </div>
  <div class="hint" style="margin-top:6px">
    「等伺服器」= 送出請求到收到第一個位元組（含連線建立與 ASP.NET 產頁）；
    「傳輸」= 把 body 收完。兩者都是每筆平均。
    「抓下」是真正流過網路的位元組（gzip 壓縮後），「原始HTML」是解壓後的完整頁面
    —— 關掉 gzip 的方法這兩欄會一樣。「實際需要」是那 5 個欄位。
  </div>
</div>

<div class="card">
  <h2>資料 · 篩選與匯出</h2>
  <div class="row">
    <div><label>發布時間 從</label><input id="d1" type="date"></div>
    <div><label>到</label><input id="d2" type="date"></div>
    <div><label>關鍵字（標題／內容／機關）</label>
         <input id="q" type="search" style="width:220px"></div>
    <button class="ghost" id="fil">篩選</button>
    <button class="ghost" id="clr">清除</button>
    <span style="flex:1"></span>
    <button class="ghost" id="xls">匯出 Excel</button>
    <button class="ghost" id="csv">匯出 CSV</button>
    <button class="ghost" id="jsn">匯出 JSON</button>
  </div>
  <div class="hint"><span class="badge" id="cnt">–</span>
    　Excel 已設好欄寬與篩選；CSV 是 UTF-8 BOM，直接開不會亂碼。</div>
  <div class="hint" id="dist" style="margin-top:2px"></div>
  <div class="danger">
    <b>清空資料</b>
    <span>清空前會自動備份成 <code>faq_備份_日期時間.db</code>，可以還原。</span>
    <span style="flex:1"></span>
    <button class="ghost warn" id="clrbody">只清內文（保留清單）</button>
    <button class="ghost warn" id="clrall">全部清空</button>
  </div>
  <div class="wrap" style="margin-top:10px">
    <table><thead><tr><th>發布時間</th><th>更新時間</th><th>檢視時間</th>
      <th>下版日期</th><th>標題</th><th>發布機關</th>
      <th class="num sep">抓下<br><span style="font-weight:400;color:#9aa2ac">byte</span></th>
      <th class="num">需要<br><span style="font-weight:400;color:#9aa2ac">byte</span></th>
      <th>內容</th></tr></thead><tbody id="tb"></tbody></table>
  </div>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
// 小於 10KB 直接顯示位元組（「實際需要」那欄只有一千多 B，
// 換算成 KB 會全部變成 1.0 KB，看不出差別）
const kb=b=> b<10240?Math.round(b).toLocaleString()+' B'
           : b<1048576?(b/1024).toFixed(1)+' KB'
                      :(b/1048576).toFixed(1)+' MB';
const secs=s=> s<90?s.toFixed(1)+' s'
             : s<5400?(s/60).toFixed(1)+' 分':(s/3600).toFixed(1)+' 小時';
let METHODS={};

fetch('/api/methods').then(r=>r.json()).then(d=>{
  METHODS=d.methods;
  $('m').innerHTML=Object.entries(METHODS).map(([k,v])=>
    `<option value="${k}">${k} · ${v.label}</option>`).join('');
  $('m').value=d.default; showDesc();
});
$('m').onchange=showDesc; $('n').oninput=showDesc;
$('iv').onchange=()=>{ $('ivwrap').hidden = $('iv').value!=='-1'; showDesc(); };
$('ivc').oninput=showDesc;
function interval(){ const v=+$('iv').value; return v<0 ? +$('ivc').value : v; }
function showDesc(){
  const v=METHODS[$('m').value]; if(!v)return;
  const iv=interval(), n=+$('n').value;
  let t=v.desc;
  if(iv>0){
    const rps=1000/iv, mins=n/rps/60;
    t+=`　限速 ${iv}ms（約 ${rps.toFixed(1)} req/s）`
      +`　→ ${n} 筆至少需要 ${mins<1?(n/rps).toFixed(0)+' 秒':mins.toFixed(0)+' 分'}`;
  } else {
    t+='　⚠ 沒有限速，會用最快速度抓';
  }
  $('desc').textContent=t;
}

$('go').onclick=()=>{
  $('go').disabled=true; $('stop').disabled=false;
  $('msg').textContent=''; $('msg').className='hint';
  $('pg').hidden=false; $('pg').value=0; $('stats').hidden=true;
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({method:$('m').value,n:+$('n').value,
                         from:$('cf').value,to:$('ct').value,
                         interval_ms:interval()})})
   .then(r=>r.json()).then(d=>{ d.ok?poll():fail(d.error); });
};
$('stop').onclick=()=>{ $('stop').disabled=true;
  $('msg').textContent='停止中…'; fetch('/api/stop',{method:'POST'}); };
$('recon').onclick=()=>{ $('msg').textContent='清單對帳中…（只翻清單，約 15 秒）';
  fetch('/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({interval_ms:interval()})}).then(r=>r.json()).then(d=>{
      if(!d.ok)$('msg').textContent=d.error; }); };

function fail(e){ $('go').disabled=false; $('stop').disabled=true;
  $('pg').hidden=true; $('msg').className='hint err'; $('msg').textContent='失敗：'+e; }

function poll(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    if(s.error) return fail(s.error);
    $('pg').max=s.total||1; $('pg').value=s.done;
    if(s.running){
      $('msg').textContent=`${s.phase}　${s.log}`
        +(s.total?`　${s.done}/${s.total}`:'')
        +(s.eta>1?`　剩約 ${secs(s.eta)}`:'');
      return setTimeout(poll,400);
    }
    $('pg').hidden=true; $('go').disabled=false; $('stop').disabled=true;
    render(s); // ---- 工具面板 ----
let TOOLS={};
fetch('/api/tools').then(r=>r.json()).then(d=>{
  d.tools.forEach(t=>TOOLS[t.key]=t);
  $('toolbtns').innerHTML=d.tools.map(t=>
    `<button class="ghost tool" data-k="${t.key}">${t.label}</button>`).join('');
  document.querySelectorAll('button.tool').forEach(b=>{
    b.onmouseenter=()=>$('tooldesc').textContent=TOOLS[b.dataset.k].desc;
    b.onclick=()=>runTool(b.dataset.k);
  });
  $('tooldesc').textContent='把滑鼠移到按鈕上看說明';
});
function runTool(k){
  document.querySelectorAll('button.tool').forEach(b=>b.disabled=true);
  $('toolout').hidden=false; $('toolout').textContent='啟動中…';
  fetch('/api/tool/'+k,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(!d.ok){ $('toolout').textContent='無法執行：'+d.error;
               document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
               return; }
    pollTool();
  });
}
function pollTool(){
  fetch('/api/tools').then(r=>r.json()).then(d=>{
    const s=d.state;
    $('toolout').textContent=s.out||'（還沒有輸出）';
    $('toolout').scrollTop=$('toolout').scrollHeight;
    if(s.running){ setTimeout(pollTool,600); return; }
    document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
    if(s.rc!==null && s.rc!==0)
      $('toolout').textContent+=`\n\n（結束代碼 ${s.rc}）`;
    loadOv(); load();
  });
}

loadOv(); load();
  });
}
function render(s){
  $('stats').hidden=false;
  $('s_time').textContent=secs(s.elapsed);
  $('s_each').textContent=(s.elapsed/Math.max(s.done,1)*1000).toFixed(0)+' ms';
  $('s_rps').textContent=(s.reqs/Math.max(s.elapsed,.001)).toFixed(1);
  $('s_wire').textContent=kb(s.wire);
  $('s_raw').textContent=kb(s.raw);
  $('s_keep').textContent=kb(s.kept);
  const q=Math.max(s.reqs,1);
  $('s_wait').textContent=(1000*(s.t_wait||0)/q).toFixed(0)+' ms';
  $('s_read').textContent=(1000*(s.t_read||0)/q).toFixed(0)+' ms';
  $('msg').className='hint';
  $('msg').textContent=`${s.method}　${s.log}　於 ${s.finished_at}`
    +`　下載 ${kb(s.wire)}，只留 ${kb(s.kept)}`
    +`（原始頁面的 ${(100*s.kept/Math.max(s.raw,1)).toFixed(1)}%）`;
}

function loadOv(){
  fetch('/api/overview').then(r=>r.json()).then(o=>{
    $('ov').innerHTML=(o.stamp
        ? `官網 <b>${o.site_total}</b> 筆　已下載 <b>${o.listed}</b> 筆`
          +(o.settled
              ?` <span style="color:#1a7f37">✓ 已收齊</span>`
              :` <span style="color:#c02626">⚠ 可能還有漏</span>`)
          +(o.delisted?`　已下架保留 ${o.delisted} 筆`:'')+'　│　'
        : '<span style="color:#c02626">清單還沒對帳過</span>　')
      +`資料庫 ${o.total} 筆，其中 ${o.with_body} 筆有內文`
      +(o.lo?`　發布時間 ${o.lo} ~ ${o.hi}`:'');
    if(o.lo && !$('d1').placeholder){ $('d1').min=$('d2').min=$('cf').min=$('ct').min=o.lo;
      $('d1').max=$('d2').max=$('cf').max=$('ct').max=o.hi; }
    $('rt').innerHTML=o.runs.map(r=>{
      const d=Math.max(r.done,1), q=Math.max(r.reqs,1);
      // 有效率 = 需要 / 實際下載（不是拿原始 HTML 算，
      // 否則關掉 gzip 的方法看起來會跟有 gzip 的一樣）
      const eff=r.wire?100*r.kept/r.wire:0;
      const na='<span style="color:#c3c8ce">–</span>';
      return `<tr>
      <td>${r.ts}</td><td>${r.method} · ${esc(r.label)}</td>
      <td class="num">${r.interval_ms?r.interval_ms+' ms':
          '<span style="color:#c3c8ce">無</span>'}</td>
      <td>${esc((r.note||'').replace(/　限速.*/,''))}</td>
      <td class="num">${r.done}</td>
      <td class="num">${secs(r.elapsed)}</td>
      <td class="num">${(r.elapsed/d*1000).toFixed(0)} ms</td>
      <td class="num sep">${r.t_wait?(r.t_wait/q*1000).toFixed(0)+' ms':na}</td>
      <td class="num">${r.t_read?(r.t_read/q*1000).toFixed(0)+' ms':na}</td>
      <td class="num sep">${kb(r.wire/d)}</td>
      <td class="num">${kb(r.raw/d)}</td>
      <td class="num">${Math.round(100*r.wire/Math.max(r.raw,1))}%</td>
      <td class="num sep">${r.kept?kb(r.kept/d)
        +(r.kept_max?`<br><span style="color:#9aa2ac;font-size:11px">`
          +`${r.kept_min}~${r.kept_max} B</span>`:''):na}</td>
      <td class="num">${r.kept?eff.toFixed(2)+'%<br><span style="color:#9aa2ac;font-size:11px">1/'
        +Math.round(r.wire/r.kept)+'</span>':na}</td>
      <td>${r.stopped?'<span style="color:#c0392b">中斷</span>':''}</td>
    </tr>`}).join('')
      ||'<tr><td colspan="15" style="color:#9aa2ac">還沒有紀錄</td></tr>';
  });
}
$('runcsv').onclick=()=>location='/api/runs.csv';
$('smpcsv').onclick=()=>location='/api/samples.csv';
$('delruns').onclick=()=>{ if(confirm('清空歷次測試紀錄？資料不會動。'))
  fetch('/api/runs',{method:'DELETE'}).then(loadOv); };

function qs(){ const p=new URLSearchParams();
  if($('d1').value)p.set('from',$('d1').value);
  if($('d2').value)p.set('to',$('d2').value);
  if($('q').value)p.set('q',$('q').value); return p; }
function load(){
  const p=qs(); p.set('limit',300);
  fetch('/api/rows?'+p).then(r=>r.json()).then(d=>{
    $('cnt').textContent=`符合 ${d.total} 筆`
      +(d.total>d.rows.length?`（表格顯示前 ${d.rows.length} 筆，匯出為全部）`:'');
    const med=a=>{const x=[...a].sort((p,q)=>p-q);return x[x.length>>1]||0};
    const nd=d.rows.map(r=>r.bytes_needed||0).filter(Boolean);
    const wr=d.rows.map(r=>r.bytes_wire||0).filter(Boolean);
    $('dist').innerHTML = nd.length
      ? `這 ${nd.length} 筆　`
        + `抓下 中位 <b>${med(wr).toLocaleString()}</b> byte`
        + `（${Math.min(...wr).toLocaleString()}~${Math.max(...wr).toLocaleString()}）`
        + `　　需要 中位 <b>${med(nd).toLocaleString()}</b> byte`
        + `（${Math.min(...nd).toLocaleString()}~${Math.max(...nd).toLocaleString()}）`
        + `　　中位浪費 <b>${Math.round(med(wr)/med(nd))} 倍</b>`
      : '';
    $('tb').innerHTML=d.rows.map(r=>`<tr>
      <td>${r.published||''}</td><td>${r.updated||''}</td>
      <td>${r.reviewed||'<span style="color:#c3c8ce">–</span>'}</td>
      <td>${r.expire||'<span style="color:#c3c8ce">–</span>'}</td>
      <td class="t"><a href="${r.url}" target="_blank" rel="noopener">${esc(r.title)}</a></td>
      <td>${esc(r.dept||'')}</td>
      <td class="num sep">${(r.bytes_wire||0).toLocaleString()}</td>
      <td class="num">${(r.bytes_needed||0).toLocaleString()}</td>
      <td class="b">${esc((r.answer||'').slice(0,140))}${(r.answer||'').length>140?'…':''}</td>
    </tr>`).join('')||'<tr><td colspan="9" style="color:#9aa2ac">沒有資料</td></tr>';
  });
}
$('fil').onclick=load;
$('clr').onclick=()=>{$('d1').value=$('d2').value=$('q').value='';load();};
function wipe(mode,label){
  const n=$('cnt').textContent;
  if(!confirm(`確定要${label}嗎？\n\n目前 ${n}\n清空前會自動備份，但這個動作無法在畫面上還原。`))
    return;
  const t=prompt('確認的話，請輸入「清空」兩個字：');
  if(t!=='清空'){ alert('已取消'); return; }
  fetch('/api/faq?mode='+mode,{method:'DELETE'}).then(r=>r.json()).then(d=>{
    if(!d.ok){ alert('失敗：'+d.error); return; }
    alert(`已${label}\n\n清空前：${d.before.total} 筆清單、${d.before.body} 筆有內文`
      +`\n清空後：${d.after.total} 筆清單、${d.after.body} 筆有內文`
      +`\n\n備份檔：${d.backup}`);
    // ---- 工具面板 ----
let TOOLS={};
fetch('/api/tools').then(r=>r.json()).then(d=>{
  d.tools.forEach(t=>TOOLS[t.key]=t);
  $('toolbtns').innerHTML=d.tools.map(t=>
    `<button class="ghost tool" data-k="${t.key}">${t.label}</button>`).join('');
  document.querySelectorAll('button.tool').forEach(b=>{
    b.onmouseenter=()=>$('tooldesc').textContent=TOOLS[b.dataset.k].desc;
    b.onclick=()=>runTool(b.dataset.k);
  });
  $('tooldesc').textContent='把滑鼠移到按鈕上看說明';
});
function runTool(k){
  document.querySelectorAll('button.tool').forEach(b=>b.disabled=true);
  $('toolout').hidden=false; $('toolout').textContent='啟動中…';
  fetch('/api/tool/'+k,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(!d.ok){ $('toolout').textContent='無法執行：'+d.error;
               document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
               return; }
    pollTool();
  });
}
function pollTool(){
  fetch('/api/tools').then(r=>r.json()).then(d=>{
    const s=d.state;
    $('toolout').textContent=s.out||'（還沒有輸出）';
    $('toolout').scrollTop=$('toolout').scrollHeight;
    if(s.running){ setTimeout(pollTool,600); return; }
    document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
    if(s.rc!==null && s.rc!==0)
      $('toolout').textContent+=`\n\n（結束代碼 ${s.rc}）`;
    loadOv(); load();
  });
}

loadOv(); load();
  });
}
$('clrall').onclick=()=>wipe('all','全部清空');
$('clrbody').onclick=()=>wipe('content','只清內文');
$('xls').onclick=()=>location='/api/export.xlsx?'+qs();
$('csv').onclick=()=>location='/api/export?fmt=csv&'+qs();
$('jsn').onclick=()=>location='/api/export?fmt=json&'+qs();
// ---- 工具面板 ----
let TOOLS={};
fetch('/api/tools').then(r=>r.json()).then(d=>{
  d.tools.forEach(t=>TOOLS[t.key]=t);
  $('toolbtns').innerHTML=d.tools.map(t=>
    `<button class="ghost tool" data-k="${t.key}">${t.label}</button>`).join('');
  document.querySelectorAll('button.tool').forEach(b=>{
    b.onmouseenter=()=>$('tooldesc').textContent=TOOLS[b.dataset.k].desc;
    b.onclick=()=>runTool(b.dataset.k);
  });
  $('tooldesc').textContent='把滑鼠移到按鈕上看說明';
});
function runTool(k){
  document.querySelectorAll('button.tool').forEach(b=>b.disabled=true);
  $('toolout').hidden=false; $('toolout').textContent='啟動中…';
  fetch('/api/tool/'+k,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(!d.ok){ $('toolout').textContent='無法執行：'+d.error;
               document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
               return; }
    pollTool();
  });
}
function pollTool(){
  fetch('/api/tools').then(r=>r.json()).then(d=>{
    const s=d.state;
    $('toolout').textContent=s.out||'（還沒有輸出）';
    $('toolout').scrollTop=$('toolout').scrollHeight;
    if(s.running){ setTimeout(pollTool,600); return; }
    document.querySelectorAll('button.tool').forEach(b=>b.disabled=false);
    if(s.rc!==null && s.rc!==0)
      $('toolout').textContent+=`\n\n（結束代碼 ${s.rc}）`;
    loadOv(); load();
  });
}

loadOv(); load();
// 閒置時也每 5 秒問一次，這樣不管工作是從哪裡啟動的（網頁、命令列、
// 另一個分頁），畫面都會自己接上去。
let watching=false;
function idleWatch(){
  if(watching) return;
  fetch('/api/status').then(r=>r.json()).then(s=>{
    if(s.running && !watching){
      watching=true;
      $('go').disabled=true; $('stop').disabled=false; $('pg').hidden=false;
      poll();
    }
  }).catch(()=>{});
}
const _poll=poll;
poll=function(){ watching=true; _poll(); };
setInterval(idleWatch, 5000);
idleWatch();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qp = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")

        if u.path == "/api/tools":
            with TOOL_LOCK:
                st = dict(TOOL)
            return self._json({
                "tools": [{"key": k, "label": v[0], "desc": v[2]}
                          for k, v in TOOLS.items()],
                "state": st})

        if u.path == "/api/methods":
            return self._json({
                "default": DEFAULT_METHOD,
                "methods": {k: {"label": v[0], "desc": v[1]}
                            for k, v in METHODS.items()}})

        if u.path == "/api/overview":
            return self._json(overview())

        if u.path == "/api/status":
            with LOCK:
                return self._json(dict(JOB))

        if u.path == "/api/rows":
            f, t, q = qp.get("from", ""), qp.get("to", ""), qp.get("q", "")
            return self._json({"total": len(query(f, t, q)),
                               "rows": query(f, t, q, qp.get("limit"))})

        if u.path == "/api/samples.csv":
            con = db()
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM samples ORDER BY run_id, seq")]
            con.close()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["run_id"] + [zh for _, zh in SAMPLE_COLS])
            for r in rows:
                w.writerow([r["run_id"]]
                           + [cell(en, r[en]) for en, _ in SAMPLE_COLS])
            data = "\ufeff".encode() + buf.getvalue().encode()
            return self._send(200, data, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename='
                               f'"samples_{time.strftime("%Y%m%d_%H%M")}.csv"'})

        if u.path == "/api/runs.csv":
            con = db()
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM runs ORDER BY id")]
            con.close()
            buf = io.StringIO()
            if rows:
                # 除了原始欄位，把常用的每筆平均值也一起算好，方便直接畫圖
                extra = ["ms_per_item", "ms_wait_per_req", "ms_read_per_req",
                         "ms_parse_per_item", "raw_per_item", "wire_per_item",
                         "kept_per_item", "efficiency_pct", "req_per_sec",
                         "compress_pct"]
                w = csv.DictWriter(buf, fieldnames=list(rows[0]) + extra)
                w.writeheader()
                for r in rows:
                    d = max(r["done"] or 0, 1)
                    q = max(r["reqs"] or 0, 1)
                    e = max(r["elapsed"] or 0, 1e-9)
                    r.update(
                        ms_per_item=round(r["elapsed"] / d * 1000, 1),
                        ms_wait_per_req=round((r["t_wait"] or 0) / q * 1000, 1),
                        ms_read_per_req=round((r["t_read"] or 0) / q * 1000, 1),
                        ms_parse_per_item=round((r["t_parse"] or 0) / d * 1000, 2),
                        raw_per_item=round(r["raw"] / d),
                        wire_per_item=round(r["wire"] / d),
                        kept_per_item=round((r["kept"] or 0) / d),
                        efficiency_pct=round(100 * (r["kept"] or 0)
                                             / max(r["raw"], 1), 3),
                        req_per_sec=round(r["reqs"] / e, 2),
                        compress_pct=round(100 * r["wire"]
                                           / max(r["raw"], 1), 1))
                    w.writerow(r)
            data = "﻿".encode() + buf.getvalue().encode()
            return self._send(200, data, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; '
                               f'filename="runs_{time.strftime("%Y%m%d_%H%M")}.csv"'})

        if u.path == "/api/export.xlsx":
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Alignment, Font, PatternFill
                from openpyxl.utils import get_column_letter
            except ImportError:
                return self._send(200, "需要 openpyxl：pip3 install openpyxl",
                                  "text/plain; charset=utf-8")
            rows = query(qp.get("from", ""), qp.get("to", ""), qp.get("q", ""))
            wb = Workbook()
            ws = wb.active
            ws.title = "常見問答"
            hdr = [zh for _, zh in FIELDS + EXPORT_EXTRA]
            ws.append(hdr)
            for r in rows:
                ws.append([xl_safe(r[en]) for en, _ in FIELDS + EXPORT_EXTRA])
            # 標題做成超連結，點下去直接開該筆在官網的頁面。
            ti = hdr.index("標題") + 1
            link = Font(color="0563C1", underline="single")
            for i, r in enumerate(rows, start=2):
                if r.get("url"):
                    cell = ws.cell(row=i, column=ti)
                    cell.hyperlink = r["url"]
                    cell.font = link
            for i in range(1, len(hdr) + 1):
                cell = ws.cell(row=1, column=i)
                cell.fill = PatternFill("solid", fgColor="1F4E5F")
                cell.font = Font(color="FFFFFF", bold=True, size=10)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            widths = {"發布時間": 12, "更新時間": 17, "檢視時間": 17,
                      "下版日期": 12, "標題": 52, "發布機關": 24,
                      "內容": 80, "附件": 40, "附件數": 8,
                      "抓下位元數": 12, "網頁原始位元數": 14,
                      "需要位元數": 12, "網址": 46}
            for i, h in enumerate(hdr, 1):
                ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 14)
            for name in ("內容", "附件"):
                ci = hdr.index(name) + 1
                for row in range(2, ws.max_row + 1):
                    ws.cell(row=row, column=ci).alignment = Alignment(
                        wrap_text=True, vertical="top")
            buf = io.BytesIO()
            wb.save(buf)
            return self._send(
                200, buf.getvalue(),
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet",
                {"Content-Disposition": 'attachment; filename='
                 f'"faq_{time.strftime("%Y%m%d_%H%M")}.xlsx"'})

        if u.path == "/api/export":
            rows = query(qp.get("from", ""), qp.get("to", ""), qp.get("q", ""))
            stamp = time.strftime("%Y%m%d_%H%M")
            if qp.get("fmt") == "json":
                data = json.dumps(
                    [{zh: r[en] for en, zh in FIELDS + EXPORT_EXTRA}
                     for r in rows], ensure_ascii=False, indent=1).encode()
                name, ct = f"faq_{stamp}.json", "application/json"
            else:
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow([zh for _, zh in FIELDS + EXPORT_EXTRA])
                for r in rows:
                    w.writerow([r[en] for en, _ in FIELDS + EXPORT_EXTRA])
                data = "﻿".encode() + buf.getvalue().encode()
                name, ct = f"faq_{stamp}.csv", "text/csv"
            return self._send(200, data, f"{ct}; charset=utf-8",
                              {"Content-Disposition":
                               f'attachment; filename="{name}"'})

        self._send(404, "{}")

    def do_POST(self):
        if self.path == "/api/stop":
            ABORT.set()
            with LOCK:
                JOB["log"] = "停止中…"
            return self._json({"ok": True})

        if self.path.startswith("/api/tool/"):
            key = self.path.rsplit("/", 1)[-1]
            if key not in TOOLS:
                return self._json({"ok": False, "error": "沒有這個工具"})
            with TOOL_LOCK:
                if TOOL["running"]:
                    return self._json({"ok": False,
                                       "error": f"「{TOOL['name']}」還在跑"})
            run_tool(key)
            return self._json({"ok": True})

        if self.path == "/api/run":
            ln = int(self.headers.get("Content-Length", 0))
            b = json.loads(self.rfile.read(ln) or "{}")
            m = b.get("method", DEFAULT_METHOD)
            if m not in METHODS:
                return self._json({"ok": False, "error": "沒有這個抓法"})
            n = max(1, min(int(b.get("n") or 100), 8400))
            iv = max(0.0, min(float(b.get("interval_ms") or 0), 10000))
            ok = start_job(m, n, b.get("from", ""), b.get("to", ""), iv)
            return self._json({"ok": ok,
                               "error": "" if ok else "已經有一個工作在跑"})

        if self.path == "/api/reconcile":
            ln = int(self.headers.get("Content-Length", 0))
            b = json.loads(self.rfile.read(ln) or "{}")
            iv = max(0.0, min(float(b.get("interval_ms") or 333), 10000))
            ok = start_reconcile(iv)
            return self._json({"ok": ok,
                               "error": "" if ok else "已經有一個工作在跑"})
        self._send(404, "{}")

    def do_DELETE(self):
        if self.path.startswith("/api/faq"):
            qp = {k: v[0] for k, v in urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).items()}
            with LOCK:
                if JOB["running"]:
                    return self._json({"ok": False,
                                       "error": "有工作正在跑，先停止再清空"})
            # 清空前一定先備份，這是不可逆操作的最後一道防線
            import shutil
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bak = os.path.join(HERE, f"faq_備份_{stamp}.db")
            shutil.copy2(DB, bak)
            con = db()
            before = con.execute(
                "SELECT COUNT(*), COUNT(NULLIF(answer,'')) FROM faq").fetchone()
            mode = qp.get("mode", "all")
            if mode == "content":       # 只清內文，保留清單
                con.execute("UPDATE faq SET answer=NULL, answer_html=NULL, "
                            "bytes_needed=NULL, bytes_wire=NULL, "
                            "bytes_raw=NULL, fetched_at=NULL")
            else:                       # 全部清掉
                con.execute("DELETE FROM faq")
            con.commit()
            after = con.execute(
                "SELECT COUNT(*), COUNT(NULLIF(answer,'')) FROM faq").fetchone()
            con.close()
            return self._json({
                "ok": True, "backup": os.path.basename(bak),
                "before": {"total": before[0], "body": before[1]},
                "after": {"total": after[0], "body": after[1]}})

        if self.path == "/api/runs":
            con = db()
            con.execute("DELETE FROM runs")
            con.commit()
            con.close()
            return self._json({"ok": True})
        self._send(404, "{}")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    db()
    url = f"http://127.0.0.1:{PORT}"
    print(f"開好了 → {url}    （Ctrl-C 結束）")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        Server(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n再見")
