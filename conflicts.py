#!/usr/bin/env python3
"""
跨條文找「同一件事、數字卻不一樣」。

    python conflicts.py            # 重算並寫出 docs/conflicts.json
    python conflicts.py --show 30  # 另外印出前 30 組看看

不是比整篇文章像不像，而是比句子：兩篇主題可以完全不同，只要各有一句在講
同一件事（例如「檢查費用由衛生局補助每人一年一次 ___ 元」），數字卻不同，
就列出來。比的數字有三種：金額、期間（天、工作天、月、小時…）、電話。

做法：
  1. 每篇切成句子，只留含金額／期間／電話的句子
  2. 把句子裡的數字遮掉，變成「句型」
  3. 句型夠像（字的兩兩組合重疊 ≥ 60%）就算在講同一件事
  4. 同一件事裡數字不一樣 → 一組

會有誤報（本來就該不同的，例如汽車和機車罰款不同、不同停車場費率不同），
所以只負責列出來給人看，不負責判定誰對誰錯。已知的誤報來源都在下面擋掉了：
日期、時刻、公文字號、計算範例、標題寫明「○年○月○日前」的舊制說明。
"""

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "docs", "faq.json")
OUT = os.path.join(HERE, "docs", "conflicts.json")

SIM = 0.6            # 句型相似門檻
TITLE_SIM = 0.2      # 金額、期間還要求兩篇標題有點像，擋掉「同一套範本套在不同對象」
MAX_DF = 200         # 太常見的字組不拿來找候選（只拿來算相似度）

# ---- 先遮掉會被誤認成數字的東西 ---------------------------------------------
DATE = re.compile(r"\d{2,4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日號])?"
                  r"|\d{1,2}\s*月\s*\d{1,2}\s*[日號]"
                  r"|\d{2,4}\s*[./]\s*\d{1,2}\s*[./]\s*\d{1,2}")
CLOCK = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}|[上下]午\s*\d{1,2}\s*[時點](?:\s*\d{1,2}\s*分)?"
                   r"|\d{1,2}\s*[時點]\s*\d{1,2}\s*分|\d{1,2}\s*[時點](?=\s*[至到~～-])")
DOCNO = re.compile(r"第\s*[A-Za-z0-9]{5,}\s*號|字第?\s*[A-Za-z0-9]{5,}\s*號?")
EXAMPLE = re.compile(r"[（(]\s*例.*?[）)]|例[如：:].*$")

# ---- 要比的三種數字 ---------------------------------------------------------
MONEY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(萬)?\s*(\d[\d,]*)?\s*元")
DUR = re.compile(r"(\d+|[一二三四五六七八九十兩]+)\s*"
                 r"(個?工作天|個?日曆天|個?工作日|日|天|個月|週|星期|小時|年)")
PHONE = re.compile(r"(?<![\dA-Za-z])(0\d{1,2})\s*[-－)）]?\s*(\d{3,4})\s*[-－]?\s*(\d{3,4})(?!\d)")

UNIT = {"個工作天": "工作天", "工作天": "工作天", "個工作日": "工作天", "工作日": "工作天",
        "個日曆天": "日", "日曆天": "日", "天": "日", "日": "日", "個月": "個月",
        "週": "週", "星期": "週", "小時": "小時", "年": "年"}
KIND_NAME = {"money": "金額", "dur": "期間", "phone": "電話"}

CN = {c: i for i, c in enumerate("零一二三四五六七八九")}
CN["兩"] = 2


def cn2int(s):
    if s.isdigit():
        return int(s)
    total = cur = 0
    for c in s:
        if c in CN:
            cur = CN[c]
        elif c in "十百千":
            total += (cur or 1) * {"十": 10, "百": 100, "千": 1000}[c]
            cur = 0
    return total + cur


def money_val(m):
    a = float(m.group(1).replace(",", ""))
    if m.group(2):
        a *= 10000
        if m.group(3):
            a += float(m.group(3).replace(",", ""))
    return f"{int(a):,} 元" if a == int(a) else f"{a:,} 元"


def scrub(s):
    """把日期、時刻、公文字號、計算範例先換掉，後面才不會誤抓"""
    s = EXAMPLE.sub("", s)
    s = DATE.sub("〔日期〕", s)
    s = CLOCK.sub("〔時刻〕", s)
    return DOCNO.sub("〔字號〕", s)


def values(s, raw=None):
    """s 是 scrub 過的句子。回傳 {種類: 值的集合}。
    給了 raw（dict）就順便記下原文怎麼寫的，網頁要拿來標黃底。"""
    out = {"money": set(), "dur": set(), "phone": set()}
    if raw is None:
        raw = {}
    for k in out:
        raw.setdefault(k, set())
    if "=" in s or "＝" in s:          # 計算式，裡面的數字是範例
        return out
    for m in PHONE.finditer(s):
        out["phone"].add(f"{m.group(1)}-{m.group(2)}{m.group(3)}")
        raw["phone"].add(m.group(0).strip())
    t = PHONE.sub("〔電話〕", s)
    for m in MONEY.finditer(t):
        out["money"].add(money_val(m))
        raw["money"].add(m.group(0).strip())
    t = MONEY.sub("〔金額〕", t)
    for m in DUR.finditer(t):
        # 「年」只收期間（3年內、5年以上），不收年份；「日」前面是「月」就是日期
        if m.group(2) == "年" and not re.match(r"\s*(內|以上|以下|後|間|為限)", t[m.end():m.end() + 3]):
            continue
        n = cn2int(m.group(1))
        if 0 < n <= 1000:
            out["dur"].add(f"{n} {UNIT[m.group(2)]}")
            raw["dur"].add(m.group(0).strip())
    return out


def pattern(s):
    """數字遮掉、標點拿掉，剩下的就是句型"""
    s = PHONE.sub("☎", s)
    s = MONEY.sub("＄", s)
    s = DUR.sub(lambda m: "#" + UNIT[m.group(2)], s)
    s = re.sub(r"\d+(\.\d+)?", "#", s)
    return re.sub(r"[\s，,、。：:（）()「」『』【】；;.．\-－~～/]", "", s)


def grams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


# 標題寫明是舊制（「108年7月1日前」「修法前」），數字本來就跟現行的不一樣
OLD_RULE = re.compile(r"\d+\s*年.*?[前止]\s*[)）]|以前|修法前|舊制")


def split_sentences(text):
    # 條列（1. 2. 一、二、）也要切開，不然同一句會混到下一條的數字。
    # 前一個字是數字就不切：「02-87916654、西區」的「54、」不是第 54 點
    for s in re.split(r"[。\n；;]|(?<=[^\d\s\-－])\s*(?=(?:\d{1,2}|[一二三四五六七八九十]{1,2})[、.．](?!\d))", text or ""):
        s = (s or "").strip()
        if len(s) >= 8:
            yield s


def find(recs):
    live = [r for r in recs if not r.get("gone") and r.get("kind") != "external"]
    sents = []
    for r in live:
        tg = grams(re.sub(r"\W", "", r.get("title") or ""))
        old = bool(OLD_RULE.search(r.get("title") or ""))
        for s in split_sentences(r.get("answer")):
            c = scrub(s)
            raw = {}
            v = values(c, raw)
            if not any(v.values()):
                continue
            p = pattern(c)
            if len(p) < 10:
                continue
            sents.append({"r": r, "s": s, "v": v, "g": grams(p), "tg": tg, "old": old,
                          "raw": raw})

    df = collections.Counter(g for x in sents for g in x["g"])
    idx = collections.defaultdict(list)
    for i, x in enumerate(sents):
        for g in x["g"]:
            if df[g] <= MAX_DF:
                idx[g].append(i)

    # 句型夠像的連起來（union-find），每一群就是「在講同一件事」的句子
    parent = list(range(len(sents)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, x in enumerate(sents):
        seen = set()
        for g in x["g"]:
            if df[g] > MAX_DF:
                continue
            for j in idx[g]:
                if j <= i or j in seen:
                    continue
                seen.add(j)
                y = sents[j]
                if y["r"]["sid"] == x["r"]["sid"] or x["old"] or y["old"]:
                    continue
                if jaccard(x["g"], y["g"]) < SIM:
                    continue
                # 電話看句型就夠；金額、期間還要標題有點像
                same_kind = [k for k in x["v"] if x["v"][k] and y["v"][k]]
                if not same_kind:
                    continue
                if same_kind != ["phone"] and jaccard(x["tg"], y["tg"]) < TITLE_SIM:
                    continue
                parent[root(i)] = root(j)

    clusters = collections.defaultdict(list)
    for i in range(len(sents)):
        clusters[root(i)].append(sents[i])

    groups = []
    for members in clusters.values():
        if len({m["r"]["sid"] for m in members}) < 2:
            continue
        for kind in ("money", "dur", "phone"):
            have = [m for m in members if m["v"][kind]]
            if len({m["r"]["sid"] for m in have}) < 2:
                continue
            combos = {frozenset(m["v"][kind]) for m in have}
            if len(combos) < 2:
                continue
            items, seen_sid = [], set()
            for m in have:
                if m["r"]["sid"] in seen_sid:
                    continue
                seen_sid.add(m["r"]["sid"])
                items.append({"sid": m["r"]["sid"], "text": m["s"],
                              "values": sorted(m["v"][kind]),
                              "marks": sorted(m["raw"][kind], key=len, reverse=True)})
            if len({tuple(i["values"]) for i in items}) < 2:
                continue
            groups.append({"kind": kind, "name": KIND_NAME[kind], "items": items})
    # 牽涉的篇數少的排前面：兩篇互相矛盾比一整批範本分兩派更可能是真的錯
    groups.sort(key=lambda g: (len(g["items"]), g["kind"], g["items"][0]["sid"]))
    return groups


def bad_phones(recs):
    """臺北市 02 開頭的電話一定是 8 碼，少一碼多一碼就是打錯。
    412 開頭的是悠遊卡、工商憑證那種全區統一號碼，本來就是 7 碼，不算。"""
    out = []
    for r in recs:
        if r.get("gone") or r.get("kind") == "external":
            continue
        seen = set()
        for s in split_sentences(r.get("answer")):
            for m in PHONE.finditer(scrub(s)):
                num = m.group(2) + m.group(3)
                if m.group(1) != "02" or len(num) == 8 or num.startswith("412"):
                    continue
                phone = f"02-{num}"
                if phone not in seen:
                    seen.add(phone)
                    out.append({"sid": r["sid"], "phone": phone, "digits": len(num),
                                "text": s, "marks": [m.group(0).strip()]})
    return out


def write(recs, path=OUT):
    res = {"groups": find(recs), "bad_phones": bad_phones(recs)}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--show", type=int, default=0, help="印出前 N 組")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    with open(DATA, encoding="utf-8") as f:
        recs = json.load(f)
    by_sid = {r["sid"]: r for r in recs}
    res = write(recs)
    groups = res["groups"]
    n = len({i["sid"] for g in groups for i in g["items"]})
    kinds = collections.Counter(g["name"] for g in groups)
    print(f"數字不一致 {len(groups)} 組，牽涉 {n} 篇問答　"
          + "　".join(f"{k} {v}" for k, v in kinds.items()))
    bp = res["bad_phones"]
    print(f"02 電話位數不對 {len(bp)} 處：" + "、".join(sorted({b["phone"] for b in bp})))
    print(f"已寫出 {os.path.relpath(OUT, HERE)}")
    for g in groups[:a.show]:
        print(f"\n【{g['name']}】")
        for it in g["items"]:
            r = by_sid[it["sid"]]
            print(f"  {'、'.join(it['values'])}　{r.get('dept')}｜{r.get('title', '')[:32]}")
            print(f"      {it['text'][:140]}")


if __name__ == "__main__":
    main()
