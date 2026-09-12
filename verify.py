#!/usr/bin/env python3
"""
內容完整性驗證 —— 確認存下來的內文沒有被截斷。

    python3 verify.py                 # 只做本機結構檢查（不連網，秒殺）
    python3 verify.py --sample 30     # 另外隨機抽 30 筆重抓比對（會連網）
    python3 verify.py --sample 30 --longest   # 抽最長的 30 筆（最容易出問題）

做三種檢查：
  1. 結構檢查（本機）：空內容、疑似被截斷的結尾、超過 Excel 單格上限
  2. 重抓比對（連網）：重新抓頁面、重新抽取，跟資料庫裡的逐字比對
  3. 對照抽法：換一種抽法（取整塊 area-essay）再比一次，
     兩種抽法結果一致才算真的沒漏
"""

import argparse
import re
import sqlite3
import time

import requests

import faqlib as F

DB = F.DB
EXCEL_CELL_LIMIT = 32_767      # Excel 單一儲存格的字元上限


def extract_current(page):
    """現在程式在用的抽法（就是 faqlib 那份，確保驗的是真正在用的邏輯）"""
    return F.parse_detail(page)["body"]


def extract_whole(page):
    """對照抽法：從 area-essay 一路取到下一個 area- 區塊為止。
    刻意用完全不同的方式，才驗得出 regex 有沒有系統性的截斷問題。"""
    i = page.find(F.ANCHOR)
    if i < 0:
        return ""
    seg = page[i:]
    j = seg.find('<div class="area-', len(F.ANCHOR))
    return F.clean_text(seg[:j] if j > 0 else seg[:200_000])


# ---- 1. 結構檢查（不連網）--------------------------------------------------
ENDINGS = "。」』）)】.!?！？：:；;～~"


def structural(con):
    rows = con.execute(
        "SELECT sid,title,answer,url FROM faq "
        "WHERE answer IS NOT NULL AND answer<>''").fetchall()
    print(f"── 結構檢查（{len(rows):,} 筆）")
    if not rows:
        print("   資料庫是空的\n")
        return []
    over = [r for r in rows if len(r[2]) > EXCEL_CELL_LIMIT]
    # 結尾看起來像被切斷。只認真正像斷句的：以逗號、頓號、連接詞、
    # 或未閉合的括號結束。以數字結尾大多是電話或分機，不算。
    CUT_RE = re.compile(r"(?:[，、,]|[（(「『【]|"
                        r"(?:及|與|和|或|但|而|如|若|且|包括|例如))$")
    cut = [r for r in rows if r[2] and CUT_RE.search(r[2].rstrip())]
    tiny = [r for r in rows if len(r[2]) < 5]
    print(f"   超過 Excel 單格上限（{EXCEL_CELL_LIMIT:,} 字）：{len(over)} 筆"
          + ("  ← 匯出 Excel 會被截斷！" if over else "  ✓"))
    print(f"   結尾疑似被切斷：{len(cut)} 筆"
          + ("  ← 需要人工看一下" if cut else "  ✓"))
    print(f"   內容少於 5 字：{len(tiny)} 筆"
          + ("（站上本來就有很短的答案，未必是問題）" if tiny else ""))
    for r in cut[:5]:
        print(f"     · {r[1][:32]}")
        print(f"       …{r[2][-46:]!r}")
    print()
    return over + cut


# ---- 2. 重抓比對（連網）----------------------------------------------------
def resample(con, n, longest, delay):
    order = "ORDER BY LENGTH(answer) DESC" if longest else "ORDER BY RANDOM()"
    rows = con.execute(
        f"SELECT sid,url,title,answer FROM faq "
        f"WHERE answer IS NOT NULL AND answer<>'' {order} LIMIT ?",
        (n,)).fetchall()
    if not rows:
        print("── 重抓比對：資料庫是空的，跳過\n")
        return 0
    s = F.new_session()
    label = "最長" if longest else "隨機"
    print(f"── 重抓比對（{label} {len(rows)} 筆）")
    bad = 0
    for sid, url, title, stored in rows:
        time.sleep(delay)
        try:
            r = s.get(url, timeout=45)
        except requests.RequestException as e:
            print(f"   ? {title[:28]}  抓取失敗 {type(e).__name__}")
            continue
        r.encoding = "utf-8"
        if F.is_blocked(r):
            print(f"   ? {title[:28]}  拿到錯誤頁，跳過")
            continue
        now = extract_current(r.text)
        whole = extract_whole(r.text)
        if now != stored:
            # 站方可能剛好改過內容，區分「被截斷」與「內容有異動」
            kind = "被截斷" if stored and now.startswith(stored) else "內容有異動"
            bad += 1
            print(f"   ✗ {title[:30]}")
            print(f"     資料庫 {len(stored):,} 字　重抓 {len(now):,} 字　({kind})")
        elif abs(len(whole) - len(now)) > 2:
            bad += 1
            print(f"   ✗ {title[:30]}  兩種抽法不一致")
            print(f"     目前抽法 {len(now):,} 字　整塊抽法 {len(whole):,} 字")
            print(f"     目前結尾 …{now[-40:]!r}")
            print(f"     整塊結尾 …{whole[-40:]!r}")
    print(f"   {len(rows) - bad}/{len(rows)} 完全一致"
          + ("  ✓" if bad == 0 else f"　{bad} 筆有問題"))
    print()
    return bad


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--longest", action="store_true")
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    t, g = con.execute(
        "SELECT COUNT(*), COUNT(NULLIF(answer,'')) FROM faq").fetchone()
    print(f"\n資料庫：清單 {t:,} 筆，有內文 {g:,} 筆\n")
    issues = structural(con)
    bad = resample(con, a.sample, a.longest, a.delay) if a.sample else 0
    con.close()
    if not issues and not bad:
        print("結論：沒有發現截斷問題。\n")
    else:
        print(f"結論：有 {len(issues)} 筆結構可疑、{bad} 筆比對不符，請看上面明細。\n")


if __name__ == "__main__":
    main()
