#!/usr/bin/env python3
"""
臺北市政府全球資訊網「常見問答」爬蟲 —— 單檔版

    python crawl.py                補齊：站上有幾筆就抓到幾筆（預設）
    python crawl.py --full         全部重抓一遍（約 50 分鐘，抓異動用）
    python crawl.py --refresh 300  補齊之外，再輪流複查 300 筆舊資料
    python crawl.py --check        環境檢查，到新電腦先跑這個
    python crawl.py --dry-run      只看會做什麼，不寫檔

預設模式會先把整份清單抓下來（42 次請求、十幾秒），跟手上的資料比對，
**站上有、我們沒有或沒抓成功的，一筆一筆補到齊**，最後印出
「站上 N 筆 / 已下載 N 筆」。數字不一樣會明講，不會靜靜地少資料。

資料存在 docs/faq.json（一筆一行的 JSON 陣列），跟著 git 走，
所以 Mac、公司電腦、GitHub Actions 三邊看到的是同一份。

每跑一次會在 docs/runs.csv 附一列執行紀錄（耗時、請求數、被擋次數），
docs/index.html 會把它畫出來。
"""

import argparse
import csv
import json
import os
import re
import sys
import time

import requests

import faqlib as F

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
DATA = os.path.join(DOCS, "faq.json")
META = os.path.join(DOCS, "meta.json")
RUNS = os.path.join(DOCS, "runs.csv")
CHANGES = os.path.join(DOCS, "changes.csv")

RATE_MS = 333            # 每次請求的間隔。全站約 50 分鐘。
PROBE_N = 5              # 開跑前探測幾筆
# 探測中位數超過這個就跳過本次（站方正在挨罰）。
# GitHub Actions 在美國，光是跨太平洋的來回就 700ms 起跳，用本機的門檻會誤判：
# 2026-09-12 第一次在 GitHub 上跑，探測 755ms，離 800ms 只差 45ms。
# 挨罰時是 +2000ms 以上，所以放寬到 1800ms 仍然抓得到。
PROBE_LIMIT_MS = 1800 if os.environ.get("GITHUB_ACTIONS") else 800
ABORT_AFTER_BLOCKS = 5   # 連續被擋這麼多次就停，不硬衝
MAX_GONE_CHECK = 60      # 不在清單裡的超過這個數，當作清單沒抓完整，不判下架
TIMEOUT = 30
MAX_RETRY = 3

# 存進 faq.json 的欄位，刻意不存 no（序號）和 fetched_at（抓取時間）——
# 那兩個每次抓都會變，存了的話每次 commit 都是八千行全改，倉庫會爆。
# 現在只有站上真的改了內容，那一行才會變。

RUN_COLS = ["時間", "來源", "模式", "站上筆數", "已下載", "缺", "新增", "異動",
            "下架", "復原", "請求數", "被擋", "耗時秒", "每筆毫秒", "下載MB", "備註"]

# 逐筆異動紀錄。總筆數一樣不代表沒事 —— 可能同時新增一筆、下架一筆，
# 只看數字完全看不出來，所以每一筆的進出都記在這裡。
CHANGE_COLS = ["時間", "動作", "變動欄位", "發布日期", "機關", "標題", "sid", "網址"]

# 會拿來比對的欄位（順序＝異動紀錄裡列出來的順序）
WATCH = [("title", "標題"), ("dept", "機關"), ("published", "發布日期"),
         ("updated", "更新時間"), ("reviewed", "檢視時間"), ("expire", "下版日期"),
         ("maintainer", "維護單位"), ("answer", "內容"), ("files", "附件")]


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


def append_csv(path, cols, rows):
    if not rows:
        return
    os.makedirs(DOCS, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, cols)
        if new:
            w.writeheader()
        w.writerows(rows)


def where():
    return "GitHub" if os.environ.get("GITHUB_ACTIONS") else "本機"


def done(rec):
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


# ---- 抓取 -------------------------------------------------------------------
class Blocked(Exception):
    pass


class Fetcher:
    def __init__(self, rate_ms=RATE_MS):
        self.s = F.new_session(pool=2)
        self.rate = F.RateLimiter(rate_ms)
        self.reqs = 0
        self.bytes = 0
        self.blocked = 0
        self.streak = 0

    def get(self, url, detail=False):
        """回傳 HTML；被擋或失敗回 None（絕不把錯誤頁當內容寫進去）。"""
        for attempt in range(MAX_RETRY):
            self.rate.wait()
            try:
                r = self.s.get(url, timeout=TIMEOUT)
            except requests.RequestException as e:
                if attempt == MAX_RETRY - 1:
                    print(f"\r    ! 連線失敗 {type(e).__name__}" + " " * 20)
                    return None
                time.sleep(2 ** attempt)
                continue
            self.reqs += 1
            self.bytes += len(r.content)
            r.encoding = "utf-8"

            # 被擋時站方回約 3 KB 的錯誤頁，HTTP 狀態碼仍然是 200。
            # 只看狀態碼會中招 —— 曾經把 5,947 筆好內容覆蓋成空的。
            if r.status_code == 200 and not (detail and F.is_blocked(r)):
                self.streak = 0
                return r.text
            if r.status_code == 404:
                return None

            self.blocked += 1
            self.streak += 1
            if self.streak >= ABORT_AFTER_BLOCKS:
                raise Blocked(f"連續被擋 {self.streak} 次")
            time.sleep((20 if r.status_code != 200 else 3) * (attempt + 1))
        return None


def probe(fe, recs):
    """開跑前先探五筆，正在挨罰就別跑了。
    9/8 有一輪就是一開始就在挨罰，25 分鐘的資料完全不能用。"""
    urls = [r["url"] for r in list(recs.values())[:300]
            if r.get("kind") == "internal"][:PROBE_N]
    if not urls:
        return 0.0
    lat = []
    for u in urls:
        t = time.perf_counter()
        fe.get(u, detail=True)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    return lat[len(lat) // 2]


def ext_key(row):
    """連到外部網站的項目沒有站內 sid，只能拿網址當識別。但站上有兩筆不同的
    問答連到同一個網址（殯葬處那兩筆），只用網址會把它們併成一筆，
    筆數就永遠比站上少一。把標題一起算進 key。"""
    if row["kind"] != "internal" and "|" not in row["sid"]:
        row["sid"] = row["sid"] + "|" + row["list_title"]
    return row


def fetch_list(fe, page, size=F.MAX_PAGE_SIZE):
    return fe.get(f"{F.LIST_URL}&page={page}&PageSize={size}")


def list_all(fe):
    """整站清單。先讀站上印的總筆數算頁數 —— 不能靠「翻到沒東西」判斷結束，
    站方對超出範圍的頁碼會一直回最後一頁，會變成無窮迴圈。"""
    html = fetch_list(fe, 1)
    if not html:
        raise Blocked("連清單第一頁都抓不到")
    total = F.total_count(html)
    rows, seen = [], set()

    def add(page_html):
        # 清單是活的，同一筆會出現在相鄰兩頁，要依 sid 去重
        for r in F.parse_list(page_html):
            ext_key(r)
            if r["sid"] not in seen:
                seen.add(r["sid"])
                rows.append(r)

    add(html)
    pages = -(-total // F.MAX_PAGE_SIZE) if total else 1
    print(f"  站上共 {total} 筆，清單 {pages} 頁")
    for page in range(2, pages + 1):
        h = fetch_list(fe, page)
        if not h:
            break
        add(h)
        print(f"\r  清單 {page}/{pages} 頁，累計 {len(rows)} 筆", end="", flush=True)
    print()
    # 收尾再讀一次總筆數。抓清單的那兩分半鐘裡站方可能新增或下架，
    # 一開始讀到的數字已經過期，拿它比對會永遠差一兩筆。
    # 期間新增的會出現在第一頁，順便一起收。
    last = fetch_list(fe, 1)
    if last:
        add(last)
        t2 = F.total_count(last)
        if t2 and t2 != total:
            print(f"  （抓的過程中站上從 {total} 筆變成 {t2} 筆）")
            total = t2
    if total and len(rows) != total:
        print(f"  ！清單解出 {len(rows)} 筆，站上說 {total} 筆，差 {total-len(rows)} 筆")
    return rows, total


def fetch_detail(fe, item):
    """抓一筆內文，回傳要存的 record；抓不到回 None。"""
    page = fe.get(item["url"], detail=True)
    if page is None:
        return None
    d = F.parse_detail(page)
    # 用「有沒有標題」判斷是不是真的抓到。不能用「有沒有內文」——
    # 有些問答的答案本身就是一份 PDF，也有些站上只建了標題沒填內容，
    # 那些都不是失敗，用內文判斷的話它們永遠進不來。
    if not F.page_ok(d):
        return None
    return {
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


def diff_fields(old, rec):
    """哪些欄位變了。內容和附件另外註明增減多少，才看得出是小修還是改寫。"""
    out = []
    for k, label in WATCH:
        a, b = old.get(k), rec.get(k)
        if a == b:
            continue
        if k == "answer":
            d = len(b or "") - len(a or "")
            out.append(f"內容({d:+d}字)")
        elif k == "files":
            d = len(b or []) - len(a or [])
            out.append(f"附件({d:+d})" if d else "附件")
        else:
            out.append(label)
    return out


def merge(recs, rec, events):
    """寫進 recs，並把新增／異動記進 events。回傳 'new' / 'changed' / 'same'。"""
    old = recs.get(rec["sid"])
    # 空內容不覆蓋既有內容 —— 除了被擋，還有站方暫時抽掉內文的情況。
    # 寧可留舊的，也不要靜靜地把資料弄不見。
    if old and not rec["answer"] and not rec["files"]:
        if old.get("answer") or old.get("files"):
            rec["answer"] = old["answer"]
            rec["files"] = old.get("files") or []
    recs[rec["sid"]] = rec

    if old is None:
        events.append(event("新增", rec, ""))
        return "new"
    fields = diff_fields(old, rec)
    if not fields:
        return "same"
    events.append(event("異動", rec, "、".join(fields)))
    return "changed"


def event(action, rec, fields):
    return {"時間": time.strftime("%Y-%m-%d %H:%M"), "動作": action,
            "變動欄位": fields, "發布日期": (rec.get("published") or "")[:10],
            "機關": rec.get("dept", ""), "標題": rec.get("title", ""),
            "sid": rec.get("sid", ""), "網址": rec.get("url", "")}


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
                    help="補齊之外，再輪流複查 N 筆舊資料（偵測舊資料被改）")
    ap.add_argument("--rate-ms", type=int, default=RATE_MS, help="每次請求間隔毫秒")
    ap.add_argument("--check", action="store_true", help="環境檢查")
    ap.add_argument("--dry-run", action="store_true", help="只看不寫")
    ap.add_argument("--force", action="store_true", help="忽略探測結果硬跑")
    a = ap.parse_args()

    if a.check:
        sys.exit(check())

    t0 = time.perf_counter()
    recs = load_data()
    meta = load_meta()
    fe = Fetcher(a.rate_ms)
    mode = "全站" if a.full else ("補齊+複查" if a.refresh else "補齊")
    print(f"{mode}　手上 {len(recs)} 筆")

    note, new, changed, fail, total = "", 0, 0, 0, None
    gone, back, events = 0, 0, []
    try:
        ms = probe(fe, recs)
        if ms:
            print(f"  探測 {PROBE_N} 筆，中位 {ms:.0f}ms")
        if ms > PROBE_LIMIT_MS and not a.force:
            note = f"探測 {ms:.0f}ms，站方正在限流"
            raise Blocked(note + "，本次跳過")

        # --- 整份清單：站上有幾筆，這裡就看得到幾筆 ---
        items, total = list_all(fe)

        # --- 跟前一版比對：誰不見了、誰回來了 ---
        # 筆數一樣不代表沒事，可能是今天新增一筆、同時下架一筆。
        listed = {x["sid"] for x in items}
        for sid, r in recs.items():
            if sid in listed and r.get("gone"):
                r.pop("gone", None)
                events.append(event("復原", r, "重新出現在清單"))
                back += 1
                print(f"    [復原] {r.get('title','')[:38]}")

        # 「不在清單裡」不等於下架。清單是活的，抓那兩分半裡順序會位移，
        # 有筆從第 34 頁擠到第 33 頁就會被我們漏看 —— 直接標下架會冤枉它。
        # 所以逐筆去敲它的內文頁確認：還開得起來就是還在，開不起來才算下架。
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
                rec = (fetch_detail(fe, {"sid": sid, "url": r["url"]})
                       if r.get("kind") != "external" else None)
                if rec is None:
                    r["gone"] = today
                    events.append(event("下架", r, "頁面已經開不起來"))
                    gone += 1
                    print(f"    [下架] {r.get('title','')[:38]}")
                else:
                    merge(recs, rec, events)   # 還在，只是剛好被清單漏掉
                    print(f"    [仍在] {rec['title'][:38]}")

        # 外部連結沒有站內內文，清單上的資訊就是全部
        for x in items:
            if x["kind"] != "internal":
                merge(recs, {"sid": x["sid"], "kind": "external", "url": x["url"],
                             "title": x["list_title"], "dept": x["list_dept"],
                             "published": iso(x["list_date"]), "updated": "",
                             "reviewed": "", "expire": "", "maintainer": "",
                             "answer": "", "files": []}, events)

        inner = [x for x in items if x["kind"] == "internal"]
        # 預設只補「還沒抓到的」；--full 才全部重抓
        todo = inner if a.full else [x for x in inner if not done(recs.get(x["sid"]))]
        short = len(todo)

        if a.refresh > 0 and not a.full:
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
        if a.dry_run:
            for x in todo[:20]:
                print(f"    {x.get('list_date','')} {x.get('list_title','')[:40]}")
            print("（dry-run，沒有寫入）")
            return

        # --- 逐筆抓，抓不到的最後重試一輪 ---
        retry = []
        for rnd in (1, 2):
            queue, retry = (todo if rnd == 1 else retry), []
            if rnd == 2 and queue:
                print(f"  重試 {len(queue)} 筆沒抓到的…")
            for i, it in enumerate(queue, 1):
                rec = fetch_detail(fe, it)
                if rec is None:
                    retry.append(it)
                    print(f"\r    ! {it['sid']} 沒抓到" + " " * 30)
                    continue
                r = merge(recs, rec, events)
                if r == "new":
                    new += 1
                    print(f"\r    [新] {rec['published'][:10]}  "
                          f"{rec['title'][:38]}" + " " * 10)
                elif r == "changed":
                    changed += 1
                    print(f"\r    [改] {rec['updated'][:16]}  "
                          f"{rec['title'][:38]}" + " " * 10)
                if len(queue) > 50 and i % 10 == 0:
                    el = time.perf_counter() - t0
                    print(f"\r    {i}/{len(queue)}　{el/i*1000:.0f}ms/筆　"
                          f"剩約 {(len(queue)-i)*el/i/60:.0f} 分",
                          end="", flush=True)
            print()
        fail = len(retry)

    except (Blocked, KeyboardInterrupt) as e:
        note = note or f"中斷：{e}"
        print(f"\n！{note}　已抓到的會存起來，下次接著補。")

    # --- 存檔 ---
    el = time.perf_counter() - t0
    live = {k: r for k, r in recs.items() if not r.get("gone")}
    got = sum(1 for r in live.values() if done(r))
    body = sum(1 for r in live.values() if r.get("answer") or r.get("files"))
    total = total or meta.get("site_total")
    if not a.dry_run:
        save_data(recs)
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
            "站上筆數": total or "", "已下載": got,
            "缺": (total - got) if total else "", "新增": new, "異動": changed,
            "下架": gone, "復原": back,
            "請求數": fe.reqs, "被擋": fe.blocked, "耗時秒": round(el, 1),
            "每筆毫秒": round(el * 1000 / fe.reqs) if fe.reqs else 0,
            "下載MB": round(fe.bytes / 1048576, 1), "備註": note,
        }])
        append_csv(CHANGES, CHANGE_COLS, events)

    # --- 站上幾筆、我們幾筆，講清楚 ---
    if not total:
        verdict = "（這次沒讀到站上總數）"
    elif got == total:
        verdict = "★ 一致"
    elif got < total:
        verdict = f"！還差 {total - got} 筆沒抓到"
    else:
        verdict = f"！多出 {got - total} 筆（可能剛下架、還沒確認）"
    print(f"\n站上 {total} 筆　已下載 {got} 筆　{verdict}")
    print(f"有內容 {body} 筆（其餘是站上本來就沒填內容的）"
          + (f"，另有 {len(recs)-len(live)} 筆已下架但保留" if len(recs) != len(live) else ""))
    print(f"本次：新增 {new}、異動 {changed}、下架 {gone}、復原 {back}、沒抓到 {fail}；"
          f"{fe.reqs} 次請求，被擋 {fe.blocked} 次，"
          f"{el:.1f} 秒，下載 {fe.bytes/1048576:.1f} MB")
    if events:
        print(f"逐筆異動已記到 {os.path.relpath(CHANGES, HERE)}（{len(events)} 列）")
    if note or (total and got != total):
        sys.exit(2)


if __name__ == "__main__":
    main()
