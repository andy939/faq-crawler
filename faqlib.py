#!/usr/bin/env python3
"""
共用模組 —— 所有腳本共同的設定、連線、解析邏輯。

會放在這裡的東西，判準是「改了之後所有程式都該一起改」。
以前這些在五個檔案裡各複製一份，同一個 bug 得修三次還會漏。
"""

import html as _html
import os
import random
import re
import ssl
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter

# Windows 主控台預設 cp950，印繁體中文會 UnicodeEncodeError
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "faq.db")

# ---- 站台常數 ---------------------------------------------------------------
BASE = "https://www.gov.taipei"
N, SMS = "EEC70A4186D4C828", "87415A8B9CE81B16"
LIST_URL = f"{BASE}/News.aspx?n={N}&sms={SMS}"
POST_URL = LIST_URL + "&Create=1"       # 日期查詢的 POST 一定要帶 &Create=1
COUNTER_URL = f"{BASE}/GetCounter.ashx"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 被擋時站方回一個約 3 KB 的錯誤頁，而且 HTTP 狀態碼仍然是 200。
# 正常內文頁約 197 KB。小於這個門檻一律當作沒抓到。
MIN_PAGE = 50_000
MAX_PAGE_SIZE = 200                     # 站方支援的每頁最大筆數

# 要留下來的欄位（順序＝匯出時的欄位順序）
FIELDS = [("published", "發布時間"), ("updated", "更新時間"),
          ("reviewed", "檢視時間"), ("expire", "下版日期"),
          ("title", "標題"), ("dept", "發布機關"), ("answer", "內容")]


# ---- 連線 -------------------------------------------------------------------
def ssl_context():
    """gov.taipei 的憑證鏈缺 Subject Key Identifier，OpenSSL 3.x 的 RFC5280
    嚴格模式會拒絕（curl 走系統鑰匙圈所以沒事）。只關掉那個吹毛求疵的旗標，
    憑證本身仍然完整驗證 —— 不是 verify=False。"""
    c = ssl.create_default_context()
    c.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return c


class Adapter(HTTPAdapter):
    def __init__(self, *a, **k):
        self._ctx = ssl_context()
        super().__init__(*a, **k)

    def init_poolmanager(self, *a, **k):
        k["ssl_context"] = self._ctx
        return super().init_poolmanager(*a, **k)


def new_session(keep_alive=True, gzip=True, pool=8):
    s = requests.Session()
    s.mount("https://", Adapter(pool_connections=pool, pool_maxsize=pool))
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate" if gzip else "identity",
    })
    if not keep_alive:
        s.headers["Connection"] = "close"
    return s


class RateLimiter:
    """全域速率限制。排的是每個請求的「出發時刻」，不是抓完再 sleep：
    下一筆出發時刻 = 上一筆出發時刻 + 間隔。某筆超時的話下一筆不再等，
    速率上限才穩定。

    不管開幾條 thread 都共用同一個，總量固定 —— 寫成「每條 thread 各自
    sleep」的話，開三條就變三倍流量。
    """

    def __init__(self, interval_ms):
        self.interval = max(interval_ms, 0) / 1000.0
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if self.next_at < now:
                self.next_at = now
            slot = self.next_at
            # 抖動：固定間隔是機器的特徵，人不會這麼準
            self.next_at += self.interval * random.uniform(0.85, 1.15)
        d = slot - time.monotonic()
        if d > 0:
            time.sleep(d)


def is_blocked(r):
    """被擋的時候伺服器回 HTTP 200 但內容是 3 KB 的錯誤頁 ——
    只看狀態碼會中招，所以用頁面大小判斷。

    注意：不能拿「有沒有 area-essay 區塊」當條件。站上有些問答
    只建了標題、沒填內容，那種頁面也沒有 area-essay，但它是正常頁，
    誤判成被擋的話會一直重試、也永遠存不進去。"""
    return r.status_code != 200 or len(r.content) < MIN_PAGE


# ---- 解析 -------------------------------------------------------------------
TBODY_RE = re.compile(r"<tbody>(.*?)</tbody>", re.S)
TR_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
NO_RE = re.compile(r'td_Class_0"[^>]*><span>(\d+)</span>')
LINK_RE = re.compile(r'<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
DEPT_RE = re.compile(r'td_Class_2"[^>]*><span>(.*?)</span>', re.S)
DATE_RE = re.compile(r'td_Class_3"[^>]*><span>(.*?)</span>', re.S)
SID_RE = re.compile(r"[?&]s=([0-9A-Fa-f]+)")
TOTAL_RE = re.compile(r'<span class="count"><i>/</i>(\d+)</span>')
HIDDEN_RE = re.compile(r'<input type="hidden" name="(__[A-Z]+)"[^>]*value="([^"]*)"')
QUERY_RE = re.compile(r"_Query=([0-9a-f-]{36})")
ANCHOR = '<div class="area-essay page-caption-p"'
ESSAY_RE = re.compile(
    r'<div class="area-essay page-caption-p".*?<div class="p">(.*?)</div>\s*</div>\s*</div>',
    re.S)
META_RE = re.compile(r"<span\s*>([一-鿿]{2,4})：(.*?)</span>", re.S)
TITLE_RE = re.compile(r'<meta property="og:title" content="(.*?)"')
TAG_RE = re.compile(r"<[^>]+>")

# 附件區塊：<div class="group-list file-download-multiple"> … 相關檔案
FILEBLOCK_RE = re.compile(
    r'<div class="group-list file-download-multiple".*?(?=<div class="group |\Z)', re.S)
FILE_RE = re.compile(
    r'<a\s+href="(?P<url>[^"]*Download\.ashx[^"]*)"[^>]*?title="(?P<title>[^"]*)"',
    re.S)
# title 長這樣：[另開新視窗]檔名(pdf 檔)(114.60 KB)
FILEMETA_RE = re.compile(
    r"^(?:\[[^\]]*\])?(?P<name>.*?)"
    r"(?:\((?P<kind>[^()]*?檔)\))?"
    r"(?:\((?P<size>[\d.]+\s*[KMG]?B)\))?$", re.S)


def clean_text(t):
    t = _html.unescape(TAG_RE.sub("\n", t))
    t = re.sub(r"[ \t　]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def total_count(page):
    """站上每一頁都會印總筆數，用它算頁數，不要靠「翻到沒東西為止」——
    站方對超出範圍的頁碼會一直回最後一頁，會變成無窮迴圈。"""
    m = TOTAL_RE.search(page)
    return int(m.group(1)) if m else None


def parse_list(page):
    """逐 <tr> 解析。不能用跨列的 .*? —— 清單裡混著連到外部網站的項目，
    跨列比對會把它後面那筆一起吃掉，而且不會報錯，只會靜靜少資料。"""
    out = []
    tb = TBODY_RE.search(page)
    if not tb:
        return out
    for row in TR_RE.findall(tb.group(1)):
        n, a = NO_RE.search(row), LINK_RE.search(row)
        if not (n and a):
            continue
        href = _html.unescape(a.group("href"))
        internal = href.startswith("News_Content.aspx")
        d, t = DEPT_RE.search(row), DATE_RE.search(row)
        out.append({
            "no": int(n.group(1)),
            "sid": SID_RE.search(href).group(1) if internal else "EXT:" + href,
            "url": f"{BASE}/{href}" if internal else href,
            "list_title": _html.unescape(TAG_RE.sub("", a.group("title"))).strip(),
            "list_dept": _html.unescape(d.group(1)).strip() if d else "",
            "list_date": t.group(1).strip() if t else "",
            "kind": "internal" if internal else "external",
        })
    return out


def parse_files(page):
    """抓「相關檔案」區塊。有些問答的答案本身就是一份 PDF，
    內文是空的 —— 那不是抓失敗，是內容以附件形式提供。"""
    blk = FILEBLOCK_RE.search(page)
    if not blk:
        return []
    out, seen = [], set()
    for m in FILE_RE.finditer(blk.group(0)):
        url = _html.unescape(m.group("url"))
        if url in seen:
            continue
        seen.add(url)
        t = _html.unescape(m.group("title")).strip()
        mm = FILEMETA_RE.match(t)
        out.append({
            "name": (mm.group("name") or t).strip() if mm else t,
            "kind": (mm.group("kind") or "").strip() if mm else "",
            "size": (mm.group("size") or "").strip() if mm else "",
            "url": url,
        })
    return out


def parse_detail(page):
    seg = page[page.find(ANCHOR):] if ANCHOR in page else page
    meta = {k: _html.unescape(v).strip() for k, v in META_RE.findall(seg)}
    m = ESSAY_RE.search(seg)
    t = TITLE_RE.search(page)
    return {
        "title": _html.unescape(t.group(1)) if t else "",
        "published": meta.get("發布日期", ""),
        "updated": meta.get("資料更新", ""),
        "reviewed": meta.get("資料檢視", ""),
        "expire": meta.get("下版日期", ""),
        "maintainer": meta.get("資料維護", ""),
        "dept": meta.get("發布單位", "") or meta.get("資料維護", ""),
        "body": clean_text(m.group(1)) if m else "",
        "body_html": m.group(1).strip() if m else "",
        "files": parse_files(page),
    }


def has_content(d):
    """有內文、或有附件，都算是有內容。純附件的問答不能當成失敗。
    兩者皆無而頁面正常的，是站上本來就沒填 —— 見 page_ok()。"""
    return bool(d.get("body")) or bool(d.get("files"))


def page_ok(d):
    """這是不是一個成功抓到的正常頁面。錯誤頁不會有 og:title，
    所以有標題就代表抓到了 —— 即使站上本來就沒填內容。"""
    return bool(d.get("title"))


# ---- 日期 -------------------------------------------------------------------
def roc_to_iso(s):
    """115-09-08 → 2026-09-08。民國年字串直接排序會亂（96 排在 115 後面）。"""
    m = re.match(r"(\d{2,3})-(\d{2})-(\d{2})", (s or "").strip())
    return f"{int(m.group(1)) + 1911:04d}-{m.group(2)}-{m.group(3)}" if m else ""


def iso_to_roc_slash(s):
    """2025-01-01 → 114/01/01（網站查詢表單要的格式）"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (s or "").strip())
    return f"{int(m.group(1)) - 1911}/{m.group(2)}/{m.group(3)}" if m else ""


def needed_bytes(rec):
    """這一筆真正要留下來的位元數 —— 那幾個欄位的 UTF-8 長度總和。"""
    return sum(len((rec.get(k) or "").encode("utf-8")) for k, _ in FIELDS)


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")
