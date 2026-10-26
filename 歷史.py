#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FAQ 內容版本歷史 → docs/history.json，給網頁的「異動管理」用。

要回答的問題：話務人員 10/1 依 FAQ 回答，那一則 10/25 內容變了，
當初他說的對不對？—— 得知道「10/1 那天這則 FAQ 寫什麼」。官網只留最新版，
所以由我們自己每天記下「被改掉的舊版」。

    python 歷史.py --backfill   # 用 git 裡每一份 faq.json 往前推，整份重建（可重複跑，結果一樣）
    python 歷史.py              # 每日：拿 HEAD 的 faq.json 跟工作目錄的比，把被改掉的舊版記下來
    python 歷史.py --stats      # 看現在記了多少

什麼才算「內容改了」：標題、機關、答案三欄，**忽略所有空白與換行**。
2026-09-27 爬蟲改了整理文字的方式，一天內 2,092 筆的空白／換行不同，其中真的改字的只有 2 筆，
不忽略空白的話歷史會被這批假異動淹沒。附件、發布日期、更新／檢視時間不算。

時間的意義：官網沒告訴我們「何時改的」，我們只知道「某次爬取時還是舊的、下一次爬取時變新的」。
所以每個舊版記 first（第一次看到）與 last（最後一次還看到），
last 到下一版 first 之間是「無法確定」的區間，網頁會照實告訴使用者。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FAQ = os.path.join(HERE, "docs", "faq.json")
OUT = os.path.join(HERE, "docs", "history.json")

_WS = re.compile(r"\s+")          # Python 3 的 \s 包含全形空白、不換行空白


def sig(x):
    """單筆的內容簽章：標題、機關、答案，去掉所有空白後比。"""
    return hashlib.sha256(json.dumps(
        [_WS.sub("", x.get("title") or ""), _WS.sub("", x.get("dept") or ""),
         _WS.sub("", x.get("answer") or "")], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def corpus_sig(faq):
    """整份語料的簽章，用來確認「記錄的狀態」對應哪一份 faq.json。"""
    h = hashlib.sha256()
    for sid in sorted(faq):
        h.update((sid + sig(faq[sid])).encode("utf-8"))
    return h.hexdigest()[:16]


def git(*args, binary=False):
    out = subprocess.check_output(["git", *args], cwd=HERE)
    return out if binary else out.decode("utf-8")


def load_rev(rev):
    raw = git("show", f"{rev}:docs/faq.json", binary=True)
    return {x["sid"]: x for x in json.loads(raw.decode("utf-8"))}


def to_ts(ci):
    """'2026-09-12 21:17:19 +0800' → '2026-09-12 21:17'（臺北時間，commit 時間就是爬取時間）"""
    return ci[:16]


def fresh_history(since):
    # docs：內容被改掉的舊版；gone：下架的 FAQ（下架前最後的標題與內容）
    # serial：流水號。串列的第 N 個 sid 就是 #N（從 1 起算），編了就不再變、只會往後加。
    return {"since": since, "updated": since, "added": {}, "docs": {}, "gone": {},
            "serial": [], "basis": {}}


def serial_key(x):
    """編流水號的順序：發布日期由舊到新，同一天再依 sid。要可重現 ——
    同一批 FAQ 不管重建幾次，編號都必須一樣，否則編號就沒有意義了。"""
    return (x.get("published") or "", x["sid"])


def snapshot_gone(x, seen):
    """下架那一則「最後的樣子」。at 是官網／爬蟲標的下架日，seen 是我們第一次看到它下架的時間。"""
    return {"at": x.get("gone"), "seen": seen, "title": x.get("title") or "",
            "dept": x.get("dept") or "", "answer": x.get("answer") or "",
            "published": x.get("published") or "", "url": x.get("url") or ""}


def apply(hist, prev, new, prev_time, now):
    """prev → new 這一步：內容改了的，把 prev 的舊版記下來；剛下架的，把下架前的樣子記下來。
    純函式（只改 hist），方便測試。回傳「內容異動」的筆數。"""
    since, added, docs = hist["since"], hist["added"], hist["docs"]
    gone = hist.setdefault("gone", {})
    n = 0
    fresh = []                                  # 這一步新出現的，等一下依固定順序編流水號
    for sid, x in new.items():
        p = prev.get(sid)
        if p is None:
            added.setdefault(sid, now)          # 紀錄開始之後才出現的新 FAQ
            fresh.append(x)
            if x.get("gone"):
                gone[sid] = snapshot_gone(x, now)
            continue
        # 下架／復原：以「下架前最後一次看到活著的樣子」為準（p），那時內容還沒被動過
        if x.get("gone") and not p.get("gone"):
            gone[sid] = snapshot_gone({**p, "gone": x["gone"]}, now)
        elif p.get("gone") and not x.get("gone") and sid in gone:
            gone[sid]["restored"] = now
        if sig(p) == sig(x):
            continue
        d = docs.get(sid)
        if d is None:
            d = docs[sid] = {"cur_from": added.get(sid, since), "old": []}
        d["old"].append({"first": d["cur_from"], "last": prev_time,
                         "title": p.get("title") or "", "dept": p.get("dept") or "",
                         "answer": p.get("answer") or ""})
        d["cur_from"] = now
        n += 1
    have = set(hist["serial"])
    hist["serial"].extend(x["sid"] for x in sorted(fresh, key=serial_key)
                          if x["sid"] not in have)
    return n


def write(hist):
    hist["docs"] = {k: hist["docs"][k] for k in sorted(hist["docs"])}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)


def backfill():
    rows = [r.split("|") for r in git("log", "--reverse", "--format=%H|%ci", "--",
                                       "docs/faq.json").split("\n") if r]
    if not rows:
        sys.exit("git 裡找不到 docs/faq.json 的歷史")
    prev = load_rev(rows[0][0])
    prev_time = to_ts(rows[0][1])
    hist = fresh_history(prev_time)
    hist["serial"] = [x["sid"] for x in sorted(prev.values(), key=serial_key)]   # 起點的資料先編號
    for sid, x in prev.items():                 # 紀錄開始時就已經下架的
        if x.get("gone"):
            hist["gone"][sid] = snapshot_gone(x, prev_time)
    events = 0
    for sha, ci in rows[1:]:
        cur = load_rev(sha)
        events += apply(hist, prev, cur, prev_time, to_ts(ci))
        prev, prev_time = cur, to_ts(ci)
    hist["updated"] = prev_time
    hist["basis"] = {"fp": corpus_sig(prev), "note": "backfill，狀態＝最後一份 commit 的 faq.json"}
    write(hist)
    print(f"往前推完成：讀了 {len(rows)} 份 faq.json（{hist['since']} ～ {prev_time}），"
          f"記下 {events} 次內容異動、涉及 {len(hist['docs'])} 筆 FAQ，"
          f"{len(hist['added'])} 筆是紀錄開始後才新增的；下架 {len(hist['gone'])} 筆"
          f"（其中 {sum(1 for g in hist['gone'].values() if g.get('restored'))} 筆後來復原）。")


def daily():
    if not os.path.exists(OUT):
        sys.exit("還沒有 history.json，請先跑一次：python 歷史.py --backfill")
    hist = json.load(open(OUT, encoding="utf-8"))
    if "serial" not in hist:
        sys.exit("history.json 還沒有流水號（舊格式），請先用 --backfill 重建一次。")
    work = {x["sid"]: x for x in json.load(open(FAQ, encoding="utf-8"))}
    head = load_rev("HEAD")
    fp_head, fp_work = corpus_sig(head), corpus_sig(work)
    basis = hist.get("basis", {}).get("fp")
    if basis == fp_work:
        print("已是最新，沒有新的異動要記。")
        return
    if basis != fp_head:
        sys.exit("history.json 記錄的狀態跟 git 的 HEAD 對不上"
                 "（可能中間有人改過 faq.json 又沒走這支），請用 --backfill 整份重建。")
    # 上一次看到舊內容的時間 = 上次更新歷史的時間。不查 git log：GitHub 排程是淺層複製
    # （只有最新一個 commit），而且最新的 commit 可能只是改程式，不是上次爬取。
    prev_time = hist["updated"]
    now = time.strftime("%Y-%m-%d %H:%M")
    n = apply(hist, head, work, prev_time, now)
    hist["updated"] = now
    hist["basis"] = {"fp": fp_work, "note": "daily，狀態＝工作目錄的 faq.json"}
    write(hist)
    print(f"記下 {n} 筆內容異動（{prev_time} → {now}）；目前共 {len(hist['docs'])} 筆 FAQ 有舊版。")


def stats():
    hist = json.load(open(OUT, encoding="utf-8"))
    vers = sum(len(d["old"]) for d in hist["docs"].values())
    print(f"紀錄起點 {hist['since']}，更新於 {hist['updated']}")
    print(f"有舊版的 FAQ {len(hist['docs'])} 筆，舊版共 {vers} 個；紀錄開始後新增 {len(hist['added'])} 筆")
    print(f"流水號編到 #{len(hist.get('serial', []))}")
    print(f"下架 {len(hist.get('gone', {}))} 筆（其中 "
          f"{sum(1 for g in hist.get('gone', {}).values() if g.get('restored'))} 筆後來復原）")
    print(f"檔案大小 {os.path.getsize(OUT)/1024:.0f} KB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        stats()
    elif a.backfill:
        backfill()
    else:
        daily()
