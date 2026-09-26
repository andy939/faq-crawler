#!/usr/bin/env python3
"""
臺北市政府全球資訊網「常見問答」爬蟲 —— 單檔版

日常：

    python crawl.py                補齊：站上有幾筆就抓到幾筆（預設）
    python crawl.py --full         全部重抓一遍（約 50 分鐘，抓異動用）
    python crawl.py --refresh 300  補齊之外，再輪流複查 300 筆舊資料
    python crawl.py --check        環境檢查，到新電腦先跑這個

效能測試（抓法比較還沒結束，所以量測的東西全部留著）：

    python crawl.py --method E --limit 500
    python crawl.py --method C --limit 500 --rate-ms 0
    python crawl.py --from 2026-01-01 --to 2026-09-01

每次執行會產生三種紀錄：

    docs/runs.csv     這一輪的彙總（耗時、每筆、等伺服器、壓縮率、有效率…）
    docs/changes.csv  逐筆的新增／異動／下架／復原
    exports/*.csv     **逐筆原始測量**，一次執行 × 一筆各一列。
                      faq.json 會被後續執行覆蓋，這個不會 ——
                      之後想到新的分析角度，重算就好，不用重爬。

資料存在 docs/faq.json（一筆一行的 JSON 陣列），跟著 git 走，
所以 Mac、公司電腦、GitHub Actions 三邊看到的是同一份。
"""

import argparse
import csv
import html as _html
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import faqlib as F

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
EXPORTS = os.path.join(HERE, "exports")
DATA = os.path.join(DOCS, "faq.json")
# 原始 HTML 另外存一份。超連結、換行、粗體、表格在轉成純文字時就沒了，
# 官網長什麼樣要靠這一份才還原得回來。分開放是因為它比純文字大一倍多，
# 網頁只需要純文字，不該為了它多載十幾 MB。
HTML = os.path.join(DOCS, "faq_html.json")
META = os.path.join(DOCS, "meta.json")
RUNS = os.path.join(DOCS, "runs.csv")
CHANGES = os.path.join(DOCS, "changes.csv")
STOP = os.path.join(DOCS, ".stop")        # 工作台按「停止」會建這個檔

# 官方的 RSS（開放資料）。這是唯一能「便宜地」知道誰被改過的管道 ——
# 清單頁只給發布日期，內文頁才有更新時間，而 RSS 的 pubDate 就是更新時間，
# 一次請求就拿得到最近 50 筆異動。缺點是只有 50 筆，當日異動超過 50 筆會漏，
# 所以它是快篩、不是保證，兜底還是要靠全站重抓。
RSS_URL = "https://www.gov.taipei/OpenData.aspx?SN=46F4BC0B817F916D"

RATE_MS = 333            # 每次請求的間隔。全站約 50 分鐘。
PROBE_N = 5              # 開跑前探測幾筆
# 探測中位數超過這個就跳過本次（站方正在挨罰）。
# GitHub Actions 在美國，光是跨太平洋的來回就 700ms 起跳，用本機的門檻會誤判：
# 2026-09-12 第一次在 GitHub 上跑，探測 755ms，離 800ms 只差 45ms。
# 挨罰時是 +2000ms 以上，所以放寬到 1800ms 仍然抓得到。
PROBE_LIMIT_MS = 1800 if os.environ.get("GITHUB_ACTIONS") else 800
PROBE_RETRY_WAIT = 20    # 第一次探測偏高時，等幾秒再探一次才決定要不要放棄
# 連續這麼多次拿到錯誤頁就自動停。預設 0 ＝ 永不自動停，跑到完為止，
# 要停由你按「停止」。沒抓到的那幾筆本來就會跳過不覆蓋、下次補齊補回來，
# 所以硬跑不會弄壞資料 —— 程式沒有理由替你決定放棄。
# 要自動停就用 --abort-after N。
ABORT_AFTER_BLOCKS = 0
# 官網整個掛掉跟「被擋」是兩回事：擋是站方還在、只是不給抓；掛是什麼都不回。
# 2026-09-26 官網 19:54 掛掉，這裡照樣重試了 3 小時、6,268 次全失敗。
# 所以：連續 1 分鐘都失敗，就「停 1 分鐘、試 1 分鐘」輪流，停的時候完全不送請求；
# 連續 30 分鐘都失敗，就自動停止並告警。中間只要有一筆正常就回到平常的抓法。
DOWN_WINDOW = 60         # 停、試各幾秒
DOWN_GIVE_UP = 30 * 60   # 連續失敗多久就放棄
MAX_GONE_CHECK = 60      # 不在清單裡的超過這個數，當作清單沒抓完整，不判下架
TIMEOUT = 30
MAX_RETRY = 3

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
DEFAULT_METHOD = "B"     # 日常補齊只抓幾筆，循序就夠，最低調

# 存進 faq.json 的欄位，刻意不存 no（序號）和 fetched_at（抓取時間）——
# 那兩個每次抓都會變，存了的話每次 commit 都是八千行全改，倉庫會爆。
# 逐筆的量測資料改存 exports/，那裡不進版本控制。

RUN_COLS = ["時間", "來源", "模式", "抓法", "併發", "限速ms", "範圍",
            "站上筆數", "已下載", "缺", "新增", "異動", "下架", "復原",
            "處理筆數", "請求數", "清單請求", "內文請求", "被擋", "錯誤",
            "耗時秒", "每筆毫秒", "請求每秒",
            "等伺服器ms", "傳輸ms", "解析ms",
            "抓下MB", "原始MB", "需要KB", "需要最小B", "需要最大B",
            "壓縮率%", "有效率%", "延遲p50ms", "延遲p95ms", "備註"]

# 逐筆原始測量的欄位（exports/*.csv）
# 「結果」和「起算秒」是拿來分析限流的：錯誤頁與失敗也要留一列，
# 不然被擋的那些完全沒紀錄，就沒東西可以看「什麼時候開始被罰、罰多久」。
SAMPLE_COLS = ["seq", "起算秒", "結果", "sid", "no", "抓法", "併發",
               "標題", "機關", "發布日期", "更新時間", "內容字數", "附件數",
               "抓下bytes", "原始bytes", "需要bytes",
               "等伺服器ms", "傳輸ms", "解析ms", "總計ms", "thread", "時間"]

# 逐筆異動紀錄。總筆數一樣不代表沒事 —— 可能同時新增一筆、下架一筆，
# 只看數字完全看不出來，所以每一筆的進出都記在這裡。
CHANGE_COLS = ["時間", "動作", "變動欄位", "發布日期", "機關", "標題", "sid", "網址"]

# 會拿來比對的欄位（順序＝異動紀錄裡列出來的順序）
WATCH = [("title", "標題"), ("dept", "機關"), ("published", "發布日期"),
         ("updated", "更新時間"), ("reviewed", "檢視時間"), ("expire", "下版日期"),
         ("maintainer", "維護單位"), ("answer", "內容"), ("files", "附件")]


# ---- 給網頁工作台看的即時進度 ------------------------------------------------
def emit(**kv):
    """工作台.py 會設 CRAWL_JSON=1，這時每隔一下就吐一行機器讀的進度，
    網頁上那八個指標卡就是靠它。命令列直接跑的時候完全不印。"""
    if os.environ.get("CRAWL_JSON"):
        # 前面補換行：進度是用 \r 原地更新、不換行的，不補的話會黏在一起
        sys.stdout.write("\n@@" + json.dumps(kv, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def alert(msg):
    """要人注意的事：工作台的紀錄框會看到這一行，另外送一個 alert 指標。"""
    print(f"\n！！【告警 {time.strftime('%H:%M:%S')}】{msg}", flush=True)
    emit(alert=msg)


def stopped():
    return os.path.exists(STOP)


# ---- 資料檔 -----------------------------------------------------------------
def load_data():
    if not os.path.exists(DATA):
        return {}
    with open(DATA, encoding="utf-8") as f:
        return {r["sid"]: r for r in json.load(f)}


def save_data(recs):
    """一筆一行寫出。是合法 JSON 陣列，但因為一行一筆，
    git 只會記下真的有改的那幾行，倉庫不會愈長愈肥。"""
    os.makedirs(DOCS, exist_ok=True)
    rows = [recs[k] for k in sorted(recs)]
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("[\n")
        for i, r in enumerate(rows):
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            f.write(",\n" if i < len(rows) - 1 else "\n")
        f.write("]\n")
    os.replace(tmp, DATA)


def load_html():
    if not os.path.exists(HTML):
        return {}
    with open(HTML, encoding="utf-8") as f:
        return json.load(f)


def save_html(h):
    """一筆一行，理由跟 save_data 一樣：git 只記真的有改的那幾行。"""
    os.makedirs(DOCS, exist_ok=True)
    tmp = HTML + ".tmp"
    keys = sorted(h)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("{\n")
        for i, k in enumerate(keys):
            f.write(json.dumps(k, ensure_ascii=False) + ": "
                    + json.dumps(h[k], ensure_ascii=False))
            f.write(",\n" if i < len(keys) - 1 else "\n")
        f.write("}\n")
    os.replace(tmp, HTML)


def load_meta():
    if os.path.exists(META):
        with open(META, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_meta(m):
    os.makedirs(DOCS, exist_ok=True)
    with open(META, "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def append_csv(path, cols, rows, bom=False):
    """附加到 CSV。欄位改過的話會自動把整個檔案搬成新格式 ——
    不然新列是照新欄位寫的、檔頭卻還是舊的，讀出來整個錯位，
    而且不會報錯，只會看到一張對不起來的表。

    bom=True 給要用 Excel 開的檔：中文 Windows 的 Excel 看不到 BOM
    會當成 Big5，整份變亂碼。docs/ 底下的不加，網頁讀的時候第一欄會多個 BOM。"""
    if not rows:
        return
    enc = "utf-8-sig" if bom else "utf-8"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old_rows, head = [], None
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            head = rd.fieldnames
            if head != cols:
                old_rows = list(rd)          # 舊列，等下用新欄位重寫一次
    if head is None or head == cols:
        with open(path, "a", encoding=enc, newline="") as f:
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            if head is None:
                w.writeheader()
            w.writerows(rows)
        return
    with open(path, "w", encoding=enc, newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in old_rows:
            w.writerow({k: r.get(k, "") for k in cols})
        w.writerows(rows)
    print(f"（{os.path.basename(path)} 欄位有變動，已搬成新格式，"
          f"舊的 {len(old_rows)} 列都留著）")


def where():
    return "GitHub" if os.environ.get("GITHUB_ACTIONS") else "本機"


def done_ok(rec):
    """這筆算不算已經下載好了。外部連結沒有站內內文，清單資訊就是全部；
    站內的只要有標題就代表抓到了正常頁（錯誤頁沒有 og:title），
    即使站上本來就沒填內容。"""
    if not rec:
        return False
    return rec.get("kind") == "external" or bool(rec.get("title"))


# ---- 日期 -------------------------------------------------------------------
DT_RE = re.compile(r"^\s*(\d{2,3})-(\d{2})-(\d{2})(\s+\d{2}:\d{2})?")


def iso(s):
    """民國日期（可帶時間）轉西元：115-09-07 11:16 → 2026-09-07 11:16。
    民國年字串直接排序會亂（96 會排在 115 後面），所以一律轉完再存。"""
    m = DT_RE.match(s or "")
    if not m:
        return (s or "").strip()
    return (f"{int(m.group(1)) + 1911:04d}-{m.group(2)}-{m.group(3)}"
            f"{m.group(4) or ''}")


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)]


# ---- 抓取 -------------------------------------------------------------------
class Blocked(Exception):
    pass


class Fetcher:
    """一次執行用一個。負責連線策略、限速、重試、擋錯誤頁，以及量測。

    量測把一次請求拆成兩段：
      等伺服器（送出 → 收到 header，含連線建立與伺服器產頁）
      傳輸　　（收到 header → 收完 body）
    兩段分開才看得出「慢是慢在伺服器還是慢在頻寬」——
    9/6 那輪就是靠這個看出關掉 gzip 慢的是傳輸、不是伺服器。
    """

    def __init__(self, rate_ms=RATE_MS, method=DEFAULT_METHOD,
                 abort_after=ABORT_AFTER_BLOCKS):
        self.abort_after = abort_after
        self.t0 = time.perf_counter()     # 逐筆測量的「起算秒」用這個當原點
        self.method = method
        label, _, kw = METHODS[method]
        self.label = label
        self.keep_alive = kw.get("keep_alive", True)
        self.gzip = kw.get("gzip", True)
        self.workers = kw.get("workers", 1)
        self.warm = kw.get("warm_session", False)
        self.counter = kw.get("hit_counter", False)
        self.per_req = kw.get("per_request_session", False)
        self.per_thr = kw.get("per_thread_session", False)

        self.rate = F.RateLimiter(rate_ms)
        self.rate_ms = rate_ms
        self.lock = threading.Lock()
        self.tl = threading.local()
        self.shared = F.new_session(self.keep_alive, self.gzip,
                                    pool=max(2, self.workers + 1))
        self.reqs = self.list_reqs = 0
        self.wire = self.raw = 0
        self.t_wait = self.t_read = self.t_parse = 0.0
        self.blocked = self.errors = self.retries = self.streak = 0
        self.lat = []
        self.down_since = None    # 最後一次拿到正常頁之後，第一次失敗的時間
        self.down_phase = -1      # 目前在第幾個「試／停」區段，用來只告警一次

    # -- 官網沒反應時的「停 1 分鐘、試 1 分鐘」--
    def ok(self):
        with self.lock:
            since, self.down_since, self.down_phase = self.down_since, None, -1
            self.streak = 0
        if since is not None and time.perf_counter() - since >= DOWN_WINDOW:
            alert(f"官網恢復回應（中斷約 {(time.perf_counter() - since) / 60:.0f} 分鐘），繼續抓")

    def bad(self):
        with self.lock:
            if self.down_since is None:
                self.down_since = time.perf_counter()

    def gate(self):
        """每次送請求前呼叫。官網沒反應的話，停的區段在這裡等，
        累計太久就丟 Blocked 收尾。三條 thread 共用同一個時鐘，一起停一起試。"""
        while True:
            if stopped():
                raise Blocked("使用者按了停止")
            since = self.down_since
            if since is None:
                return
            t = time.perf_counter() - since
            if t >= DOWN_GIVE_UP:
                raise Blocked(f"官網 {DOWN_GIVE_UP // 60} 分鐘沒有回應，自動停止")
            phase = int(t // DOWN_WINDOW)
            with self.lock:
                first, self.down_phase = phase > self.down_phase, max(phase, self.down_phase)
            left = (DOWN_GIVE_UP - t) / 60
            if phase % 2 == 0:                     # 試的區段：照常送
                if first and phase:
                    alert(f"再試 {DOWN_WINDOW} 秒看官網回來沒（還剩約 {left:.0f} 分鐘就放棄）")
                return
            if first:
                alert(f"官網 {t / 60:.0f} 分鐘沒有正常回應，先停 {DOWN_WINDOW} 秒"
                      f"（還剩約 {left:.0f} 分鐘就放棄）")
            time.sleep(1)

    # -- 連線 --
    def session(self):
        if self.per_req:
            return F.new_session(self.keep_alive, self.gzip)
        if self.per_thr:
            s = getattr(self.tl, "s", None)
            if s is None:
                s = self.tl.s = F.new_session(self.keep_alive, self.gzip)
            return s
        return self.shared

    def warm_up(self):
        """方法 D：先進清單頁拿 cookie，後面每筆帶 Referer，像真的瀏覽器。"""
        if self.warm:
            self.get(F.LIST_URL)

    # -- 一次請求 --
    def _timed(self, fn, *a, **kw):
        """回傳 (response, 等伺服器秒, 傳輸秒, 抓下bytes, 原始bytes)。"""
        last = None
        for attempt in range(MAX_RETRY):
            self.gate()
            self.rate.wait()
            t0 = time.perf_counter()
            try:
                r = fn(*a, stream=True, timeout=TIMEOUT, **kw)
                t1 = time.perf_counter()
                body = r.content
                t2 = time.perf_counter()
            except requests.RequestException as e:
                last = e
                self.bad()
                with self.lock:
                    self.retries += 1
                if attempt == MAX_RETRY - 1:
                    break
                time.sleep(2 ** attempt + random.random())
                continue
            r.encoding = "utf-8"
            wire = int(r.headers.get("Content-Length") or len(body))
            with self.lock:
                self.reqs += 1
                self.wire += wire
                self.raw += len(body)
                self.t_wait += t1 - t0
                self.t_read += t2 - t1
                self.lat.append(t2 - t0)
            return r, t1 - t0, t2 - t1, wire, len(body)
        with self.lock:
            self.errors += 1
        raise last or RuntimeError("請求失敗")

    def get(self, url, detail=False, referer=None):
        """回傳 HTML；被擋或失敗回 None（絕不把錯誤頁當內容寫進去）。"""
        hdr = {"Referer": referer} if referer else {}
        for attempt in range(MAX_RETRY):
            try:
                r, _, _, _, _ = self._timed(self.session().get, url, headers=hdr)
            except Blocked:
                raise
            except Exception:
                return None
            # 被擋時站方回約 3 KB 的錯誤頁，HTTP 狀態碼仍然是 200。
            # 只看狀態碼會中招 —— 曾經把 5,947 筆好內容覆蓋成空的。
            if r.status_code == 200 and not (detail and F.is_blocked(r)):
                self.ok()
                return r.text
            if r.status_code == 404:
                self.ok()                 # 404 也是官網有在回應
                return None
            self.bad()
            with self.lock:
                self.blocked += 1
                self.streak += 1
                streak = self.streak
            if self.abort_after and streak >= self.abort_after:
                raise Blocked(f"連續被擋 {streak} 次")
            time.sleep((20 if r.status_code != 200 else 3) * (attempt + 1))
        return None

    # -- 一筆內文（含量測）--
    def detail(self, item):
        """回傳 (record, sample)。抓不到時 record 是 None，但 sample 還是有 ——
        錯誤頁和連線失敗都留一列，那才是分析限流的原始資料。"""

        def failed(why, tw=0.0, tr=0.0, wire=0, raw=0):
            return None, {
                "起算秒": round(time.perf_counter() - self.t0, 2),
                "結果": why, "sid": item["sid"], "no": item.get("no", ""),
                "抓法": self.method, "併發": self.workers,
                "標題": item.get("list_title", ""), "機關": item.get("list_dept", ""),
                "發布日期": "", "更新時間": "", "內容字數": 0, "附件數": 0,
                "抓下bytes": wire, "原始bytes": raw, "需要bytes": 0,
                "等伺服器ms": round(tw * 1000, 1), "傳輸ms": round(tr * 1000, 1),
                "解析ms": 0, "總計ms": round((tw + tr) * 1000, 1),
                "thread": threading.current_thread().name,
                "時間": time.strftime("%H:%M:%S"),
            }

        s = self.session()
        ref = F.LIST_URL if self.warm else None
        hdr = {"Referer": ref} if ref else {}
        try:
            r, tw, tr, wire, raw = self._timed(s.get, item["url"], headers=hdr)
        except Blocked:
            raise
        except Exception as e:
            return failed(f"連線失敗：{type(e).__name__}")
        if F.is_blocked(r):
            self.bad()
            with self.lock:
                self.blocked += 1
                self.streak += 1
                streak = self.streak
            if self.abort_after and streak >= self.abort_after:
                raise Blocked(f"連續被擋 {streak} 次")
            # 錯誤頁約 3 KB、HTTP 仍然是 200。這一列留著才看得出
            # 「第幾秒開始被擋、連續擋了幾筆、多久解除」
            return failed(f"錯誤頁 HTTP{r.status_code} {len(r.content)}B",
                          tw, tr, wire, raw)
        self.ok()

        tp = time.perf_counter()
        d = F.parse_detail(r.text)
        t_parse = time.perf_counter() - tp
        with self.lock:
            self.t_parse += t_parse

        # 用「有沒有標題」判斷是不是真的抓到。不能用「有沒有內文」——
        # 有些問答的答案本身就是一份 PDF，也有些站上只建了標題沒填內容，
        # 那些都不是失敗，用內文判斷的話它們永遠進不來。
        if not F.page_ok(d):
            return None, None

        if self.counter:
            # 真的瀏覽器看完內文會再回報一次點閱數，方法 D 要模擬這個。
            try:
                _, cw, cr, cwire, craw = self._timed(
                    s.post, F.COUNTER_URL,
                    data={"n": F.N, "s": item["sid"], "smlsn": F.SMS},
                    headers={"Referer": item["url"]})
                tw += cw
                tr += cr
                wire += cwire
                raw += craw
            except Exception:
                pass
        if self.per_req:
            s.close()

        rec = {
            "sid": item["sid"],
            "kind": "internal",
            "url": item["url"],
            "title": d["title"] or item.get("list_title", ""),
            "dept": d["dept"] or item.get("list_dept", ""),
            "published": iso(d["published"] or item.get("list_date", "")),
            "updated": iso(d["updated"]),
            "reviewed": iso(d["reviewed"]),
            "expire": iso(d["expire"]),
            "maintainer": d["maintainer"],
            "answer": d["body"],
            "files": d["files"],
        }
        rec["_html"] = d["body_html"] or ""      # 由呼叫端撈走，不會進 faq.json
        need = sum(len((rec.get(k) or "").encode("utf-8"))
                   for k in ("published", "updated", "reviewed", "expire",
                             "title", "dept", "answer"))
        # 這一筆的位元帳，跟著資料一起存：
        #   抓下＝真正流過網路的（gzip 壓縮後的整頁）
        #   原始＝解壓後的完整 HTML
        #   需要＝上面那幾個欄位加起來的 UTF-8 長度
        # 三個擺在一起才看得出「抓了一整頁，真正要的只有幾百 bytes」。
        rec["bytes_wire"] = wire
        rec["bytes_raw"] = raw
        rec["bytes_needed"] = need
        sample = {
            "起算秒": round(time.perf_counter() - self.t0, 2), "結果": "正常",
            "sid": rec["sid"], "no": item.get("no", ""),
            "抓法": self.method, "併發": self.workers,
            "標題": rec["title"], "機關": rec["dept"],
            "發布日期": rec["published"], "更新時間": rec["updated"],
            "內容字數": len(rec["answer"]), "附件數": len(rec["files"]),
            "抓下bytes": wire, "原始bytes": raw, "需要bytes": need,
            "等伺服器ms": round(tw * 1000, 1), "傳輸ms": round(tr * 1000, 1),
            "解析ms": round(t_parse * 1000, 2),
            "總計ms": round((tw + tr + t_parse) * 1000, 1),
            "thread": threading.current_thread().name,
            "時間": time.strftime("%H:%M:%S"),
        }
        return rec, sample


def probe(fe, recs):
    """開跑前探幾筆，看站方是不是正在罰我們。

    只量「等伺服器」那一段 —— 送出請求到收到第一個位元組。以前是量整個
    fe.get() 的牆鐘時間，裡面混了三樣不該算的東西，結果在公司第一次跑
    幾乎每次都誤判成限流、抓十幾秒就停：

      1. 限速器的 sleep：每次請求前程式自己故意等 333ms
      2. 第一次連線的成本：DNS + TCP + TLS 握手，公司網路還要過 proxy
      3. 下載 197 KB 內文的時間：那是傳輸慢，不是伺服器慢

    所以現在多探一筆當暖身、那筆不計入，並且直接取 _timed 量到的 t_wait。

    只看速度還不夠：被擋的時候站方回的是 3 KB 錯誤頁，那個「很快」，
    所以順便數一下有幾筆根本不是正常內文頁。

    回傳 (中位毫秒, 每筆毫秒清單, 拿到錯誤頁的筆數)。
    """
    urls = [r["url"] for r in list(recs.values())[:300]
            if r.get("kind") == "internal"][:PROBE_N + 1]
    if not urls:
        return 0.0, [], 0
    lat, bad = [], 0
    for i, u in enumerate(urls):
        try:
            r, t_wait, _, _, _ = fe._timed(fe.session().get, u)
        except Blocked:
            raise
        except Exception:
            continue
        if i == 0:
            continue                      # 暖身那筆不算
        lat.append(t_wait * 1000)
        if F.is_blocked(r):
            bad += 1
    if not lat:
        return 0.0, [], bad
    return sorted(lat)[len(lat) // 2], lat, bad


# ---- RSS 快篩 ---------------------------------------------------------------
RSS_ITEM = re.compile(r"<item>(.*?)</item>", re.S)


def _tag(seg, tag):
    m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), seg, re.S)
    return m.group(1) if m else ""


def rss_changed(fe, recs):
    """讀官方 RSS，回傳 (要抓的清單, 提示訊息)。

    pubDate 就是站上的「資料更新」時間，所以拿它跟我們存的比，
    就知道誰動過 —— 一次請求，不用進去敲八千頁。

    只有 50 筆，所以會檢查「最舊的那筆是不是比我們上次跑的時間還新」，
    是的話代表這期間的異動可能已經被擠出榜外，會提醒你補跑全站。
    """
    from email.utils import parsedate_to_datetime

    html = fe.get(RSS_URL)
    if not html:
        raise Blocked("RSS 抓不到")
    items = RSS_ITEM.findall(html)
    if not items:
        raise Blocked("RSS 沒有內容（格式可能改了）")

    todo, seen, oldest = [], [], None
    for it in items:
        link = _html.unescape(_tag(it, "link"))
        m = re.search(r"[?&]s=([0-9A-Fa-f]+)", link)
        if not m:
            continue                      # 沒有 sid 的（外部連結）跳過
        sid = m.group(1)
        try:
            when = parsedate_to_datetime(_tag(it, "pubDate")).astimezone()
        except Exception:
            continue
        stamp = when.strftime("%Y-%m-%d %H:%M")
        oldest = stamp if oldest is None or stamp < oldest else oldest
        seen.append(stamp)
        rec = recs.get(sid)
        if rec is None or (rec.get("updated") or "")[:16] < stamp:
            todo.append({"sid": sid, "url": link, "kind": "internal",
                         "list_title": _tag(it, "title"), "list_dept": "",
                         "list_date": "", "no": ""})
    note = (f"RSS {len(items)} 筆（{oldest} ~ {max(seen) if seen else '—'}），"
            f"其中 {len(todo)} 筆要抓")
    return todo, note, oldest


# ---- 清單 -------------------------------------------------------------------
def ext_key(row):
    """連到外部網站的項目沒有站內 sid，只能拿網址當識別。但站上有兩筆不同的
    問答連到同一個網址（殯葬處那兩筆），只用網址會把它們併成一筆，
    筆數就永遠比站上少一。把標題一起算進 key。"""
    if row["kind"] != "internal" and "|" not in row["sid"]:
        row["sid"] = row["sid"] + "|" + row["list_title"]
    return row


def date_token(fe, d_from, d_to):
    """日期查詢只能 POST，GET 參數完全被忽略。action 要帶 &Create=1，
    送 __VIEWSTATE + 民國格式的起迄日。回應給一個 _Query=<GUID>，
    那個 token 是無狀態的，之後翻頁可以直接 GET，也可以平行。"""
    r, *_ = fe._timed(fe.shared.get, F.LIST_URL)
    form = {k: _html.unescape(v) for k, v in F.HIDDEN_RE.findall(r.text)}
    form.update({
        "jNewsModule_field_SDate_1": F.iso_to_roc_slash(d_from),
        "jNewsModule_field_EDate_1": F.iso_to_roc_slash(d_to),
        "jNewsModule_BtnSend": "送出查詢",
    })
    p, *_ = fe._timed(fe.shared.post, F.POST_URL, data=form)
    q = F.QUERY_RE.search(p.text)
    if not q:
        raise RuntimeError("網站沒有回傳查詢結果（日期格式可能不對）")
    hits = F.TOTAL_RE.search(p.text)
    print(f"  日期查詢 {d_from or '不限'} ~ {d_to or '不限'}："
          f"命中 {hits.group(1) if hits else '?'} 筆")
    return q.group(1)


def fetch_list(fe, page, size=F.MAX_PAGE_SIZE, token=""):
    u = f"{F.LIST_URL}&page={page}&PageSize={size}"
    if token:
        u += f"&_Query={token}"
    return fe.get(u)


def list_all(fe, limit=0, token=""):
    """整站清單。先讀站上印的總筆數算頁數 —— 不能靠「翻到沒東西」判斷結束，
    站方對超出範圍的頁碼會一直回最後一頁，會變成無窮迴圈。"""
    size = F.MAX_PAGE_SIZE if not limit else min(F.MAX_PAGE_SIZE, max(10, limit))
    html = fetch_list(fe, 1, size, token)
    if not html:
        raise Blocked("連清單第一頁都抓不到")
    total = F.total_count(html)
    rows, seen, dup = [], set(), 0

    def add(page_html):
        nonlocal dup
        # 清單是活的，同一筆會出現在相鄰兩頁，要依 sid 去重
        for r in F.parse_list(page_html):
            ext_key(r)
            if r["sid"] in seen:
                dup += 1
                continue
            seen.add(r["sid"])
            rows.append(r)

    add(html)
    want = limit or total or len(rows)
    pages = -(-want // size) if want else 1
    print(f"  站上共 {total} 筆，要抓 {want} 筆，清單 {pages} 頁")
    emit(phase="清單", total=want)
    for page in range(2, pages + 1):
        if len(rows) >= want:
            break
        h = fetch_list(fe, page, size, token)
        if not h:
            break
        before = len(rows)
        add(h)
        if len(rows) == before:      # 整頁都是看過的＝真的翻完了
            break
        print(f"\r  清單 {page}/{pages} 頁，累計 {len(rows)} 筆", end="", flush=True)
        emit(phase="清單", done=len(rows), total=want, list_reqs=fe.reqs)
    print()
    if dup:
        print(f"  清單去重：跳過 {dup} 筆重複")

    full = not limit and not token
    if full:
        # 收尾再讀一次總筆數。抓清單的那兩分半鐘裡站方可能新增或下架，
        # 一開始讀到的數字已經過期，拿它比對會永遠差一兩筆。
        # 期間新增的會出現在第一頁，順便一起收。
        last = fetch_list(fe, 1, size, token)
        if last:
            add(last)
            t2 = F.total_count(last)
            if t2 and t2 != total:
                print(f"  （抓的過程中站上從 {total} 筆變成 {t2} 筆）")
                total = t2
        if total and len(rows) != total:
            print(f"  ！清單解出 {len(rows)} 筆，站上說 {total} 筆，"
                  f"差 {total - len(rows)} 筆")
    return (rows[:limit] if limit else rows), total, full


# ---- 比對 -------------------------------------------------------------------
def diff_fields(old, rec):
    """哪些欄位變了。內容和附件另外註明增減多少，才看得出是小修還是改寫。"""
    out = []
    for k, label in WATCH:
        a, b = old.get(k), rec.get(k)
        if a == b:
            continue
        if k == "answer":
            out.append(f"內容({len(b or '') - len(a or ''):+d}字)")
        elif k == "files":
            d = len(b or []) - len(a or [])
            out.append(f"附件({d:+d})" if d else "附件")
        else:
            out.append(label)
    return out


def event(action, rec, fields):
    return {"時間": time.strftime("%Y-%m-%d %H:%M"), "動作": action,
            "變動欄位": fields, "發布日期": (rec.get("published") or "")[:10],
            "機關": rec.get("dept", ""), "標題": rec.get("title", ""),
            "sid": rec.get("sid", ""), "網址": rec.get("url", "")}


def merge(recs, rec, events, htmls=None):
    """寫進 recs，並把新增／異動記進 events。回傳 'new' / 'changed' / 'same'。"""
    raw = rec.pop("_html", None)
    if htmls is not None and raw:
        htmls[rec["sid"]] = raw
    old = recs.get(rec["sid"])
    # 空內容不覆蓋既有內容 —— 除了被擋，還有站方暫時抽掉內文的情況。
    # 寧可留舊的，也不要靜靜地把資料弄不見。
    if old and not rec["answer"] and not rec["files"]:
        if old.get("answer") or old.get("files"):
            rec["answer"] = old["answer"]
            rec["files"] = old.get("files") or []
    if old and old.get("gone"):
        rec["gone"] = old["gone"]
    recs[rec["sid"]] = rec
    if old is None:
        events.append(event("新增", rec, ""))
        return "new"
    fields = diff_fields(old, rec)
    if not fields:
        return "same"
    events.append(event("異動", rec, "、".join(fields)))
    return "changed"


# ---- 環境檢查 ---------------------------------------------------------------
def check():
    ok = True

    def line(name, good, msg):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK  ' if good else '失敗'}  {name}：{msg}")

    print("環境檢查")
    v = sys.version_info
    line("Python", v >= (3, 9), f"{v.major}.{v.minor}.{v.micro}（需要 3.9+）")
    line("requests", True, requests.__version__)
    print(f"  資訊  Proxy：{os.environ.get('HTTPS_PROXY') or '沒設定'}")
    print(f"  資訊  憑證：{os.environ.get('REQUESTS_CA_BUNDLE') or '系統預設'}")

    fe = Fetcher(rate_ms=500)
    try:
        html = fetch_list(fe, 1)
    except Exception as e:
        # 公司做 TLS 攔檢會在這裡失敗。跟資訊單位要根憑證設 REQUESTS_CA_BUNDLE，
        # 絕對不要改成 verify=False，那是真的把中間人防護關掉。
        line("連線", False, f"{type(e).__name__}: {e}")
        return 1
    line("連線", bool(html), "連得上 gov.taipei")
    if html:
        rows = F.parse_list(html)
        line("清單解析", len(rows) > 0,
             f"解出 {len(rows)} 筆，站上共 {F.total_count(html)} 筆")
        if rows:
            t = time.perf_counter()
            d = fe.get(rows[0]["url"], detail=True)
            ms = (time.perf_counter() - t) * 1000
            line("內文抓取", d is not None,
                 f"{ms:.0f}ms" + ("" if d else " ← 拿到錯誤頁，現在正在被擋"))

    recs = load_data()
    line("資料檔", True,
         f"{len(recs)} 筆" if recs else "還沒有 docs/faq.json，跑一次 crawl.py")
    print("\n" + ("全部通過。" if ok else "有項目沒過，照上面的提示處理。"))
    return 0 if ok else 1


# ---- 主流程 -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="全部重抓（約 50 分鐘）")
    ap.add_argument("--refresh", type=int, default=0,
                    help="補齊之外，再輪流複查 N 筆舊資料")
    ap.add_argument("--method", default=DEFAULT_METHOD, choices=list(METHODS),
                    help="抓法 A~F（效能比較用，預設 B）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只抓 N 筆（效能測試用，不會影響下架判斷）")
    ap.add_argument("--from", dest="d_from", default="", help="發布日期起（YYYY-MM-DD）")
    ap.add_argument("--to", dest="d_to", default="", help="發布日期迄（YYYY-MM-DD）")
    ap.add_argument("--rate-ms", type=int, default=RATE_MS, help="每次請求間隔毫秒")
    ap.add_argument("--rss", action="store_true",
                    help="只抓官方 RSS 報告有異動的（1 次請求＋幾筆，數秒）")
    ap.add_argument("--check", action="store_true", help="環境檢查")
    ap.add_argument("--dry-run", action="store_true", help="只看不寫")
    ap.add_argument("--abort-after", type=int, default=ABORT_AFTER_BLOCKS,
                    metavar="N",
                    help="連續被擋 N 次就自動停（預設 0 ＝ 不自動停，跑到完為止）")
    ap.add_argument("--gate", action="store_true",
                    help="探測到站方在限流就跳過本次（預設不判斷，設定什麼就抓什麼）")
    ap.add_argument("--force", action="store_true",
                    help="（現在預設就會抓，這個參數留著相容舊的批次檔）")
    a = ap.parse_args()

    if a.check:
        sys.exit(check())
    if os.path.exists(STOP):
        os.remove(STOP)                      # 上一輪按停留下來的

    t0 = time.perf_counter()
    recs = load_data()
    htmls = load_html()          # 原始 HTML，另外一份
    meta = load_meta()
    fe = Fetcher(a.rate_ms, a.method, a.abort_after)
    bench = bool(a.limit or a.d_from or a.d_to)
    # 這兩種模式都沒有把整份清單抓下來，所以不能拿來判斷「抓齊了沒」。
    # 完整性這件事只能由真的對過帳的那一輪來寫，不然數字會互相污染。
    no_reconcile = bench or a.rss
    mode = ("效能測試" if bench else
            "RSS快更" if a.rss else
            "全站" if a.full else
            "補齊+複查" if a.refresh else "補齊")
    scope = (f"{a.d_from or '不限'}~{a.d_to or '不限'}" if (a.d_from or a.d_to)
             else (f"前 {a.limit} 筆" if a.limit else "全部"))
    print(f"{mode}　抓法 {a.method}・{fe.label}　限速 {a.rate_ms}ms　"
          f"範圍 {scope}　手上 {len(recs)} 筆")
    emit(phase="開始", mode=mode, method=a.method, label=fe.label,
         workers=fe.workers, rate_ms=a.rate_ms)

    note, new, changed, fail = "", 0, 0, 0
    gone, back, events, samples = 0, 0, [], []
    total, got_list = None, 0
    try:
        bad = 0

        def do_probe(label=""):
            nonlocal bad
            ms, lat, bad = probe(fe, recs)
            if lat:
                print(f"  探測{label} {len(lat)} 筆（另有 1 筆暖身不計）："
                      + "、".join(f"{x:.0f}" for x in lat)
                      + f" ms，中位 {ms:.0f}ms"
                      + (f"，其中 {bad} 筆拿到錯誤頁" if bad else ""))
                emit(phase="探測", probe_ms=round(ms), probe_bad=bad)
            return ms

        ms = do_probe()
        # 探測只報數字，不攔人。保護資料的是下游那三道，它們看的是
        # 「實際發生什麼」而不是「猜」：錯誤頁不寫入、空內容不覆蓋、
        # 連續被擋 N 次才停。硬跑最壞就是慢、抓不到的下次補，資料不會壞。
        # 要恢復「先探測再決定跑不跑」的行為就加 --gate。
        if ms > PROBE_LIMIT_MS or bad:
            print(f"　（站方回應偏慢或有錯誤頁，還是照抓 ——"
                  f" 抓不到的會跳過不覆蓋，連續被擋 {ABORT_AFTER_BLOCKS} 次才停）")
        if a.gate and not a.force:
            if bad * 2 >= PROBE_N:
                note = f"探測 {PROBE_N} 筆有 {bad} 筆是錯誤頁，站方正在擋"
                raise Blocked(note + "，本次跳過")
            if ms > PROBE_LIMIT_MS:
                print(f"！探測 {ms:.0f}ms 偏高（門檻 {PROBE_LIMIT_MS}ms），"
                      f"等 {PROBE_RETRY_WAIT} 秒再探一次…")
                time.sleep(PROBE_RETRY_WAIT)
                ms = do_probe("（第二次）")
                if ms > PROBE_LIMIT_MS:
                    note = (f"探測兩次都超過 {PROBE_LIMIT_MS}ms"
                            f"（{ms:.0f}ms），站方正在限流")
                    raise Blocked(note + "，本次跳過")
                print("  第二次探測正常，繼續。")

        fe.warm_up()
        token = date_token(fe, a.d_from, a.d_to) if (a.d_from or a.d_to) else ""

        if a.rss:
            # RSS 快更：不翻清單，直接問官方「最近哪 50 筆被改過」。
            # 不做下架判斷、不做完整性對帳 —— 那要整份清單才算得準。
            rss_todo, rss_note, rss_oldest = rss_changed(fe, recs)
            print(f"  {rss_note}")
            fe.list_reqs = fe.reqs
            last = (meta.get("last_run") or "")[:16]
            if last and rss_oldest and rss_oldest > last:
                print(f"！RSS 最舊的一筆是 {rss_oldest}，比你上次跑的 {last} 還新 ——"
                      f"\n　 這中間的異動可能已經被擠出 50 筆榜外，建議補跑一次全站。")

        # --- 清單：站上有幾筆，這裡就看得到幾筆 ---
        items, total, full_list = ([], meta.get("site_total"), False) if a.rss \
            else list_all(fe, a.limit, token)
        got_list = len(items)
        fe.list_reqs = fe.reqs

        # 外部連結沒有站內內文，清單上的資訊就是全部
        for x in items:
            if x["kind"] != "internal":
                merge(recs, {"sid": x["sid"], "kind": "external", "url": x["url"],
                             "title": x["list_title"], "dept": x["list_dept"],
                             "published": iso(x["list_date"]), "updated": "",
                             "reviewed": "", "expire": "", "maintainer": "",
                             "answer": "", "files": []}, events)

        # --- 跟前一版比對：誰不見了、誰回來了 ---
        # 只有整份清單都抓完才做，抓 100 筆的效能測試不能拿來判下架。
        if full_list:
            listed = {x["sid"] for x in items}
            for sid, r in recs.items():
                if sid in listed and r.get("gone"):
                    r.pop("gone", None)
                    events.append(event("復原", r, "重新出現在清單"))
                    back += 1
                    print(f"    [復原] {r.get('title','')[:38]}")
            # 「不在清單裡」不等於下架。清單是活的，抓那兩分半裡順序會位移，
            # 有筆從第 34 頁擠到第 33 頁就會被我們漏看 —— 直接標下架會冤枉它。
            # 所以逐筆去敲它的內文頁確認：開得起來就是還在，開不起來才算下架。
            cand = [s for s, r in recs.items()
                    if s not in listed and not r.get("gone") and r.get("url")]
            if not total:
                print("  （沒讀到站上總數，這次不做下架判斷）")
            elif len(cand) > MAX_GONE_CHECK:
                print(f"  （有 {len(cand)} 筆不在清單裡，超過 {MAX_GONE_CHECK} 筆，"
                      f"像是清單沒抓完整，這次不做下架判斷）")
            elif cand:
                print(f"  清單少了 {len(cand)} 筆，逐筆確認是不是真的下架…")
                today = time.strftime("%Y-%m-%d")
                for sid in cand:
                    r = recs[sid]
                    rec = (fe.detail({"sid": sid, "url": r["url"]})[0]
                           if r.get("kind") != "external" else None)
                    if rec is None:
                        r["gone"] = today
                        events.append(event("下架", r, "頁面已經開不起來"))
                        gone += 1
                        print(f"    [下架] {r.get('title','')[:38]}")
                    else:
                        merge(recs, rec, events, htmls)
                        print(f"    [仍在] {rec['title'][:38]}")

        # --- 要抓哪些內文 ---
        inner = [x for x in items if x["kind"] == "internal"]
        if a.rss:
            todo = rss_todo                   # RSS 說有動過的才抓
        elif a.full or bench:
            todo = inner                      # 全站／效能測試：清單上的全抓
        else:
            todo = [x for x in inner if not done_ok(recs.get(x["sid"]))]
        short = len(todo)

        if a.refresh > 0 and not a.full and not bench and not a.rss:
            # 清單看不到「資料更新」時間，舊資料被改偵測不到，所以輪流複查。
            # 用 meta 裡的游標輪，不存每筆的抓取時間 —— 那會讓 git diff 變八千行。
            keys = sorted(k for k, r in recs.items() if r.get("kind") == "internal")
            have = {x["sid"] for x in todo}
            if keys:
                cur = meta.get("refresh_cursor", 0) % len(keys)
                pick = [keys[(cur + i) % len(keys)]
                        for i in range(min(a.refresh, len(keys)))]
                meta["refresh_cursor"] = (cur + len(pick)) % len(keys)
                todo += [{"sid": k, "url": recs[k]["url"], "kind": "internal"}
                         for k in pick if k not in have]

        print(f"  要抓 {len(todo)} 筆內文"
              + (f"（缺 {short} 筆 + 複查 {len(todo)-short} 筆）" if a.refresh else ""))
        emit(phase="內文", done=0, total=len(todo))
        if a.dry_run:
            for x in todo[:20]:
                print(f"    {x.get('list_date','')} {x.get('list_title','')[:40]}")
            print("（dry-run，沒有寫入）")
            return

        # --- 逐筆抓，抓不到的最後重試一輪 ---
        counter = {"n": 0}
        last_emit = [0.0]

        def progress():
            el = time.perf_counter() - t0
            n = counter["n"]
            if time.perf_counter() - last_emit[0] < 0.4:
                return
            last_emit[0] = time.perf_counter()
            emit(phase="內文", done=n, total=len(todo), elapsed=round(el, 1),
                 reqs=fe.reqs, wire=fe.wire, raw=fe.raw, blocked=fe.blocked,
                 errors=fe.errors, t_wait=round(fe.t_wait, 2),
                 t_read=round(fe.t_read, 2), t_parse=round(fe.t_parse, 3),
                 kept=sum(s["需要bytes"] for s in samples),
                 eta=round((len(todo) - n) * el / n, 1) if n else 0)

        def one(it):
            if stopped():
                raise Blocked("使用者按了停止")
            rec, sample = fe.detail(it)
            with fe.lock:
                counter["n"] += 1
                seq = counter["n"]
            if sample:
                sample["seq"] = seq
                samples.append(sample)
            progress()
            return it, rec

        retry = []
        for rnd in (1, 2):
            queue, retry = (todo if rnd == 1 else retry), []
            if rnd == 2 and queue:
                print(f"  重試 {len(queue)} 筆沒抓到的…")
            if not queue:
                continue
            if fe.workers > 1:
                with ThreadPoolExecutor(max_workers=fe.workers) as ex:
                    out = list(ex.map(one, queue))
            else:
                out = [one(i) for i in queue]
            for it, rec in out:
                if rec is None:
                    retry.append(it)
                    continue
                r = merge(recs, rec, events, htmls)
                if r == "new":
                    new += 1
                    print(f"    [新] {rec['published'][:10]}  {rec['title'][:38]}")
                elif r == "changed":
                    changed += 1
                    print(f"    [改] {rec['updated'][:16]}  {rec['title'][:38]}")
            if len(queue) > 20:
                print(f"  這一輪 {len(queue)} 筆完成，沒抓到 {len(retry)} 筆")
        fail = len(retry)

    except (Blocked, KeyboardInterrupt) as e:
        note = note or f"中斷：{e}"
        print(f"\n！{note}　已抓到的會存起來，下次接著補。")

    # --- 收尾與量測 ---
    el = time.perf_counter() - t0
    live = {k: r for k, r in recs.items() if not r.get("gone")}
    got = sum(1 for r in live.values() if done_ok(r))
    body = sum(1 for r in live.values() if r.get("answer") or r.get("files"))
    total = total or meta.get("site_total")
    lat = sorted(fe.lat)
    # 逐筆平均只算成功的那些。錯誤頁和失敗也有列（分析限流要用），
    # 但把它們算進「每筆幾毫秒」會讓數字失真。
    ok_s = [s for s in samples if s.get("結果") == "正常"]
    n_s = len(ok_s)
    kept = sum(s["需要bytes"] for s in ok_s)
    detail_reqs = fe.reqs - fe.list_reqs

    if not a.dry_run:
        save_data(recs)
        save_html(htmls)
        if not no_reconcile:   # 沒對過帳的模式不要動「現況」那幾個數字
            meta.update({
                "count": len(recs), "got": got, "with_content": body,
                "gone_total": len(recs) - len(live), "site_total": total,
                "last_run": time.strftime("%Y-%m-%d %H:%M"), "last_mode": mode,
                "last_elapsed": round(el, 1), "last_reqs": fe.reqs,
                "last_blocked": fe.blocked, "last_where": where(),
                "last_new": new, "last_changed": changed,
                "last_gone": gone, "last_back": back,
            })
            if a.full and not note:
                meta["last_full"] = meta["last_run"]
        save_meta(meta)
        append_csv(RUNS, RUN_COLS, [{
            "時間": time.strftime("%Y-%m-%d %H:%M"), "來源": where(), "模式": mode,
            "抓法": f"{a.method}・{fe.label}", "併發": fe.workers,
            "限速ms": a.rate_ms, "範圍": scope,
            "站上筆數": total or "", "已下載": got,
            "缺": (total - got) if total else "", "新增": new, "異動": changed,
            "下架": gone, "復原": back, "處理筆數": n_s,
            "請求數": fe.reqs, "清單請求": fe.list_reqs, "內文請求": detail_reqs,
            "被擋": fe.blocked, "錯誤": fe.errors,
            "耗時秒": round(el, 1),
            "每筆毫秒": round(el * 1000 / n_s) if n_s else 0,
            "請求每秒": round(fe.reqs / el, 2) if el else 0,
            "等伺服器ms": round(fe.t_wait * 1000 / fe.reqs) if fe.reqs else 0,
            "傳輸ms": round(fe.t_read * 1000 / fe.reqs) if fe.reqs else 0,
            "解析ms": round(fe.t_parse * 1000 / n_s, 2) if n_s else 0,
            "抓下MB": round(fe.wire / 1048576, 2),
            "原始MB": round(fe.raw / 1048576, 2),
            "需要KB": round(kept / 1024, 1),
            # 單筆的最小與最大，網頁那張比較表會印在「需要/筆」下面一行
            "需要最小B": min((s["需要bytes"] for s in ok_s), default=""),
            "需要最大B": max((s["需要bytes"] for s in ok_s), default=""),
            "壓縮率%": round(fe.wire / fe.raw * 100, 1) if fe.raw else 0,
            "有效率%": round(kept / fe.wire * 100, 2) if fe.wire else 0,
            "延遲p50ms": round(pct(lat, 0.5) * 1000),
            "延遲p95ms": round(pct(lat, 0.95) * 1000),
            "備註": note,
        }])
        append_csv(CHANGES, CHANGE_COLS, events)
        if samples:
            # 逐筆原始測量。faq.json 會被後續執行覆蓋，這份不會 ——
            # 之後想到新的分析角度，重算就好，不用重爬。
            name = (f"{time.strftime('%Y%m%d_%H%M%S')}_方法{a.method}"
                    f"_{n_s}筆.csv")
            append_csv(os.path.join(EXPORTS, name), SAMPLE_COLS,
                       sorted(samples, key=lambda s: s["seq"]), bom=True)
            print(f"逐筆原始測量 → exports/{name}")

    if os.path.exists(STOP):
        os.remove(STOP)

    # --- 站上幾筆、我們幾筆，講清楚 ---
    if a.rss:
        verdict = "（RSS 快更沒有翻清單，不做完整性對帳）"
        total = meta.get("site_total")
    elif bench:
        verdict = "（效能測試，不做完整性對帳）"
    elif not total:
        verdict = "（這次沒讀到站上總數）"
    elif got == total:
        verdict = "★ 一致"
    elif got < total:
        verdict = f"！還差 {total - got} 筆沒抓到"
    else:
        verdict = f"！多出 {got - total} 筆（可能剛下架、還沒確認）"
    print(f"\n站上 {total} 筆　已下載 {got} 筆　{verdict}")
    print(f"有內容 {body} 筆（其餘是站上本來就沒填內容的）"
          + (f"，另有 {len(recs)-len(live)} 筆已下架但保留"
             if len(recs) != len(live) else ""))
    print(f"本次：新增 {new}、異動 {changed}、下架 {gone}、復原 {back}、"
          f"沒抓到 {fail}；{fe.reqs} 次請求（清單 {fe.list_reqs}、內文 "
          f"{detail_reqs}），被擋 {fe.blocked} 次，錯誤 {fe.errors} 次")
    if n_s:
        print(f"量測：{el:.1f} 秒　每筆 {el*1000/n_s:.0f}ms　"
              f"等伺服器 {fe.t_wait*1000/fe.reqs:.0f}ms/次　"
              f"傳輸 {fe.t_read*1000/fe.reqs:.0f}ms/次　"
              f"解析 {fe.t_parse*1000/n_s:.2f}ms/筆")
        print(f"　　　抓下 {fe.wire/1048576:.1f} MB　原始 {fe.raw/1048576:.1f} MB"
              f"（壓縮到 {fe.wire/fe.raw*100:.0f}%）　"
              f"真正需要 {kept/1024:.0f} KB（有效率 {kept/fe.wire*100:.2f}%）")
    if events:
        print(f"逐筆異動已記到 docs/changes.csv（{len(events)} 列）")
    emit(phase="完成", done=n_s, total=n_s, elapsed=round(el, 1),
         reqs=fe.reqs, wire=fe.wire, raw=fe.raw, kept=kept,
         blocked=fe.blocked, errors=fe.errors,
         t_wait=round(fe.t_wait, 2), t_read=round(fe.t_read, 2),
         t_parse=round(fe.t_parse, 3))
    if note or (not no_reconcile and total and got != total):
        sys.exit(2)


if __name__ == "__main__":
    main()
