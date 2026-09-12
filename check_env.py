#!/usr/bin/env python3
"""
環境檢查 —— 到新電腦（例如公司的 Win10）第一件要跑的事。

    python check_env.py

一路檢查到底，每項都告訴你「過了沒」和「不過怎麼辦」：
  1. Python 版本
  2. 必要套件
  3. 對外網路與 proxy
  4. TLS 憑證（公司常做攔檢，這關最容易卡）
  5. 對外 IP（判斷是不是整棟共用）
  6. 能不能連到市府網站、抓到清單
  7. WAF 探測（現在會不會被擋）
  8. 資料庫狀態
"""

import os
import platform
import sys
import time

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
OK, WARN, BAD = "[ 過 ]", "[注意]", "[失敗]"
issues = []


def say(status, title, detail="", fix=""):
    print(f"{status} {title}")
    if detail:
        for line in detail.split("\n"):
            print(f"       {line}")
    if fix:
        print(f"       → {fix}")
        issues.append((title, fix))
    print()


print("\n" + "=" * 60)
print("  常見問答爬蟲 · 環境檢查")
print(f"  {platform.system()} {platform.release()}　{time.strftime('%Y-%m-%d %H:%M')}")
print("=" * 60 + "\n")

# ---- 1 Python ---------------------------------------------------------------
v = sys.version_info
say(OK if v >= (3, 9) else BAD,
    f"Python {v.major}.{v.minor}.{v.micro}",
    f"執行檔：{sys.executable}",
    "" if v >= (3, 9) else "需要 3.9 以上，請到 python.org 安裝新版")

# ---- 2 套件 -----------------------------------------------------------------
missing = []
for mod, pkg, need in [("requests", "requests", True),
                       ("openpyxl", "openpyxl", False)]:
    try:
        m = __import__(mod)
        say(OK, f"{pkg} {getattr(m, '__version__', '')}")
    except ImportError:
        missing.append(pkg)
        say(BAD if need else WARN, f"{pkg} 沒安裝",
            "" if need else "只影響 Excel 匯出，其他功能正常",
            f"pip install {pkg}")
if missing:
    print(f"       一次裝完：pip install {' '.join(missing)}\n")

if "requests" in missing:
    print("requests 是必要的，先裝好再跑一次。\n")
    sys.exit(1)

import requests

import faqlib as F

# ---- 3 Proxy ----------------------------------------------------------------
proxies = {k: os.environ[k] for k in
           ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
           if k in os.environ}
say(OK if not proxies else WARN,
    "Proxy 設定",
    "沒有設 proxy，直接連外" if not proxies
    else "\n".join(f"{k} = {v}" for k, v in proxies.items()),
    "" if not proxies else "requests 會自動使用，若連不上先確認 proxy 位址正確")

# ---- 4 TLS ------------------------------------------------------------------
URL = F.LIST_URL


def make():
    return F.new_session()


tls_ok = False
try:
    r = make().get(URL, timeout=30)
    tls_ok = True
    say(OK, "TLS 連線", f"HTTP {r.status_code}，收到 {len(r.content):,} bytes")
except requests.exceptions.SSLError as e:
    msg = str(e)
    if "Missing Subject Key Identifier" in msg:
        say(BAD, "TLS 憑證檢查失敗（RFC5280 嚴格模式）",
            "程式應該已經處理這個，若還出現代表版本不對",
            "確認 faqlib.py 的 ssl_context() 有 verify_flags &= ~ssl.VERIFY_X509_STRICT")
    elif "self signed" in msg or "unable to get local issuer" in msg:
        say(BAD, "TLS 憑證檢查失敗 —— 公司做了 TLS 攔檢",
            "公司的資安設備用自己的憑證換掉了原本的。\n"
            "這在企業網路很常見，不是攻擊。",
            "跟資訊單位要公司根憑證（.crt/.pem），然後設環境變數：\n"
            "         setx REQUESTS_CA_BUNDLE C:\\path\\to\\company-ca.pem\n"
            "         千萬不要改成 verify=False，那是真的關掉防護")
    else:
        say(BAD, "TLS 連線失敗", msg[:180], "把上面訊息給資訊單位看")
except requests.exceptions.ProxyError as e:
    say(BAD, "Proxy 連線失敗", str(e)[:180], "確認 HTTPS_PROXY 設定正確")
except requests.exceptions.RequestException as e:
    say(BAD, f"連線失敗（{type(e).__name__}）", str(e)[:180],
        "確認這台電腦能開 https://www.gov.taipei")

if not tls_ok:
    print("=" * 60)
    print("  連不到市府網站，後面的檢查跳過。先解決上面的問題。")
    print("=" * 60 + "\n")
    sys.exit(1)

# ---- 5 對外 IP --------------------------------------------------------------
ip = None
for u in ("https://api.ipify.org", "https://ifconfig.me/ip"):
    try:
        ip = make().get(u, timeout=15).text.strip()
        break
    except Exception:
        continue
say(OK if ip else WARN, "對外 IP",
    f"{ip}\n公司通常整棟共用一個 IP。WAF 是按 IP 計算的，\n"
    f"所以你的爬蟲流量會跟同事的瀏覽算在一起。" if ip else "查不到（不影響使用）")

# ---- 6 清單頁 ---------------------------------------------------------------
import re
try:
    r = make().get(URL + "&page=1&PageSize=200", timeout=45)
    r.encoding = "utf-8"
    total = F.total_count(r.text)
    rows = len(re.findall(r'td_Class_0"[^>]*><span>(\d+)</span>', r.text))
    if total and rows:
        say(OK, "清單頁解析", f"站上共 {total:,} 筆，這一頁抓到 {rows} 列")
    else:
        say(BAD, "清單頁解析失敗",
            f"收到 {len(r.content):,} bytes 但找不到預期的表格",
            "站方可能改版了，需要更新解析規則")
except Exception as e:
    say(BAD, "清單頁抓取失敗", f"{type(e).__name__}: {e}")

# ---- 7 WAF 探測 -------------------------------------------------------------
import sqlite3
import statistics as S

DB = F.DB
urls = []
if os.path.exists(DB):
    try:
        urls = [x[0] for x in sqlite3.connect(DB).execute(
            "SELECT url FROM faq WHERE kind='internal' ORDER BY no LIMIT 8")]
    except Exception:
        pass
if not urls:
    ids = re.findall(r'News_Content\.aspx\?n=[^&]+&sms=[^&]+&s=([0-9A-F]+)', r.text)[:8]
    urls = [f"https://www.gov.taipei/News_Content.aspx"
            f"?n=EEC70A4186D4C828&sms=87415A8B9CE81B16&s={i}" for i in ids]

if urls:
    s = make()
    lat, small = [], 0
    for u in urls:
        t = time.perf_counter()
        try:
            rr = s.get(u, timeout=45)
        except Exception:
            continue
        lat.append((time.perf_counter() - t) * 1000)
        if F.is_blocked(rr):
            small += 1
        time.sleep(0.4)
    if lat:
        med = S.median(lat)
        if small:
            say(BAD, f"WAF 探測：{small}/{len(lat)} 筆收到錯誤頁",
                f"等伺服器中位 {med:.0f}ms\n"
                f"正常內文頁約 197 KB，錯誤頁只有 3 KB —— 現在被擋著。",
                "現在不要跑大量抓取。換時段（晚上），或改跑 update.py（只打十幾次）")
        elif med > 1000:
            say(WARN, f"WAF 探測：正在被限流（{med:.0f}ms）",
                "頁面內容是對的，但每筆被加罰約 2 秒。",
                "可以跑但會很慢。建議換時段，或先跑 update.py")
        else:
            say(OK, f"WAF 探測：正常（等伺服器中位 {med:.0f}ms）",
                f"{len(lat)} 筆全部拿到完整內文頁，現在適合抓取")

# ---- 8 資料庫 ---------------------------------------------------------------
if os.path.exists(DB):
    try:
        c = sqlite3.connect(DB)
        t, g = c.execute(
            "SELECT COUNT(*), COUNT(NULLIF(answer,'')) FROM faq").fetchone()
        last = c.execute("SELECT MAX(fetched_at) FROM faq").fetchone()[0]
        say(OK, "資料庫",
            f"清單 {t:,} 筆，有內文 {g:,} 筆\n最後抓取：{last}\n"
            f"檔案大小：{os.path.getsize(DB) / 1048576:.1f} MB")
    except Exception as e:
        say(BAD, "資料庫讀取失敗", str(e)[:150], "faq.db 可能損壞，用備份還原")
else:
    say(WARN, "還沒有 faq.db",
        "第一次跑的話正常。", "python crawl_faq.py list  先建立清單")

# ---- 總結 -------------------------------------------------------------------
print("=" * 60)
if not issues:
    print("  全部通過，可以開始用了。")
    print()
    print("  日常更新     python update.py")
    print("  網頁工作台   python app.py")
    print("  抓整站       python crawl_faq.py detail")
else:
    print(f"  有 {len(issues)} 項要處理：")
    for i, (t, f) in enumerate(issues, 1):
        print(f"    {i}. {t}")
        print(f"       {f.splitlines()[0]}")
print("=" * 60 + "\n")
