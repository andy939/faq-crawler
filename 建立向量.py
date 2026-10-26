#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/faq.json 用指定的模型算成向量，存到 vectors/<代號>.npy。

    python 建立向量.py --model intfloat/multilingual-e5-small --tag e5-small
    python 建立向量.py --model BAAI/bge-m3                     --tag bge-m3
    python 建立向量.py --list        # 看已經算過哪些

增量：有舊向量、也有逐筆記錄（<代號>.state.json）時，只重算「新增或內容改過」的筆，
其餘沿用，e5-large 從一小時縮到幾十秒。條件不符（換了模型、變動超過 30%…）會自動整份重算，
也可以用 --full 強制整份重算。

    # 舊向量還沒有逐筆記錄時：指定它「當初是用哪份 faq.json 算的」，
    # 程式會驗證指紋，對不上就拒絕，不會默默用錯。
    python 建立向量.py --model intfloat/multilingual-e5-large --tag e5-large \\
           --baseline-faq 舊的faq.json

各家模型的前綴規定不一樣，弄錯分數會莫名其妙掉一截：
  e5 系列  查詢要加 "query: "、文件要加 "passage: "
  bge 中文 查詢要加一句指令，文件不加
  bge-m3   兩邊都不用加
前綴跟模型名一起存進 <代號>.json，查詢時由 檢索.py 照著用，不用在兩邊各記一次。
"""

import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
VEC = os.path.join(HERE, "vectors")

# 文件截斷長度。答案長度中位數才 140 字，1200 已經涵蓋絕大多數；
# 留這個上限是因為少數法規類答案有上萬字，整篇餵進去又慢又會稀釋重點。
MAXLEN = 1200

# 要重算的筆數超過這個比例，增量就沒有意義（而且舊向量多半不可靠），直接整份重算。
INCREMENTAL_MAX = 0.30


def faq_fingerprint(faq):
    """這份語料的指紋。只對「會影響檢索結果的欄位」取雜湊 ——
    發布日期之類的改了不影響向量，不該害大家重算。
    用 json 串接欄位，不自己拼分隔字元：欄位裡本來就可能有任何字元，
    自選的分隔符撞到的話，兩份不同的語料會算出同一個指紋。"""
    h = hashlib.sha256()
    for x in sorted(faq, key=lambda r: r["sid"]):
        h.update(json.dumps(
            [x["sid"], x.get("title") or "", x.get("dept") or "",
             x.get("answer") or ""],
            ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


def prefixes(model):
    """各家對「查詢」和「文件」要加什麼前綴的規定都不一樣，
    照著各自的模型卡來。弄錯的話模型還是會給你向量、不會報錯，
    但分數會莫名其妙掉一截 —— 這種錯最難發現。"""
    m = model.lower()
    if "qwen3-embedding" in m:
        # Qwen3 的查詢要包成指令格式，文件則原樣送進去
        return ("Instruct: Given a web search query, retrieve relevant "
                "passages that answer the query\nQuery: ", "")
    if "nomic-embed" in m:
        return "search_query: ", "search_document: "
    if "e5" in m:
        return "query: ", "passage: "
    if "bge" in m and "zh" in m:
        return "為這個句子生成表示以用於檢索相關文章：", ""
    return "", ""                      # bge-m3、其他一律不加


def load_reusable(np, R, vec, tag, model, dpre, baseline_path):
    """找得到、而且可信的舊向量就回傳 (舊矩陣, {sid: (列號, 內容雜湊)})，否則回傳 None。

    舊向量的列順序＝它當初那份 faq.json 的順序（.npy 是照 faq.json 順序存的），
    所以一定要有「每一列是哪個 sid」的記錄才敢沿用 —— 有 .state.json 就用它，
    沒有就靠 --baseline-faq 指定當初的 faq.json，並驗證指紋一致。"""
    mp = os.path.join(vec, tag + ".json")
    npy = os.path.join(vec, tag + ".npy")
    if not (os.path.exists(mp) and os.path.exists(npy)):
        print("  沒有舊向量，整份重算。")
        return None
    meta = json.load(open(mp, encoding="utf-8"))
    if (meta.get("model"), meta.get("doc_prefix"), meta.get("maxlen")) != (model, dpre, MAXLEN):
        print("  舊向量的模型／前綴／截斷長度跟這次不同，不能沿用，整份重算。")
        return None
    mat = np.load(npy)
    sp = os.path.join(vec, tag + ".state.json")
    if os.path.exists(sp):
        st = json.load(open(sp, encoding="utf-8"))
        rows, hashes = st["rows"], st["hashes"]
        src = "逐筆記錄"
    elif baseline_path:
        bfaq = json.load(open(baseline_path, encoding="utf-8"))
        if faq_fingerprint(bfaq) != meta.get("faq_fingerprint"):
            sys.exit(f"錯誤：--baseline-faq 的指紋是 {faq_fingerprint(bfaq)}，"
                     f"舊向量記錄的是 {meta.get('faq_fingerprint')}，不是同一份資料，"
                     "拒絕沿用（沿用會讓向量對錯筆）。")
        rows = [x["sid"] for x in bfaq]
        hashes = [R.doc_hash(x) for x in bfaq]
        src = f"--baseline-faq（指紋 {meta['faq_fingerprint']} 一致）"
    else:
        print("  舊向量沒有逐筆記錄（.state.json），這次整份重算；算完之後就能增量。"
              "\n  （想這次就增量：加 --baseline-faq 指定它當初用的 faq.json。）")
        return None
    if len(rows) != len(mat) or len(rows) != len(hashes):
        print(f"  逐筆記錄（{len(rows)} 筆）與向量（{len(mat)} 筆）對不上，整份重算。")
        return None
    print(f"  沿用舊向量（{len(mat):,} 筆，建於 {meta.get('built_at', '?')}，對照來源：{src}）")
    return mat, {s: (i, h) for i, (s, h) in enumerate(zip(rows, hashes))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="Hugging Face 模型名稱")
    ap.add_argument("--tag", help="代號，用來當檔名與 /api/search 的參數")
    ap.add_argument("--list", action="store_true", help="列出已經算過的")
    ap.add_argument("--full", action="store_true", help="不增量，整份重算")
    ap.add_argument("--baseline-faq", help="舊向量當初用的 faq.json（舊向量沒有逐筆記錄時用）")
    ap.add_argument("--faq", help="要算的 faq.json（預設 docs/faq.json）")
    ap.add_argument("--vec-dir", help="向量存放資料夾（預設 vectors/）")
    a = ap.parse_args()

    vec = a.vec_dir or VEC
    os.makedirs(vec, exist_ok=True)
    if a.list or not a.model:
        rows = [f for f in os.listdir(vec)
                if f.endswith(".json") and not f.endswith(".state.json")]
        if not rows:
            print("還沒有算過任何向量。")
        for f in sorted(rows):
            m = json.load(open(os.path.join(vec, f), encoding="utf-8"))
            print(f"  {m['tag']:16} {m['model']:42} {m['dim']:>5} 維　"
                  f"{m['count']:,} 筆　{m['built_at']}")
        return
    if not a.tag:
        ap.error("要給 --tag")

    import numpy as np
    import 檢索 as R

    faq = json.load(open(a.faq or os.path.join(DOCS, "faq.json"), encoding="utf-8"))
    n = len(faq)
    qpre, dpre = prefixes(a.model)
    print(f"模型 {a.model}")
    print(f"  查詢前綴 {qpre!r}　文件前綴 {dpre!r}")

    hashes = [R.doc_hash(x) for x in faq]
    texts = [dpre + R.doc_text(x).replace("\n", " ")[:MAXLEN] for x in faq]

    # ---- 決定哪些要算 ----------------------------------------------------
    reuse = None if a.full else load_reusable(
        np, R, vec, a.tag, a.model, dpre, a.baseline_faq)
    todo = list(range(n))
    if reuse:
        old_mat, old = reuse
        todo = [i for i, x in enumerate(faq)
                if x["sid"] not in old or old[x["sid"]][1] != hashes[i]]
        if len(todo) > INCREMENTAL_MAX * n:
            print(f"  要重算 {len(todo):,} 筆（{len(todo)/n:.0%}），超過 {INCREMENTAL_MAX:.0%}，"
                  "改整份重算。")
            reuse, todo = None, list(range(n))
        else:
            gone = len(set(old) - {x["sid"] for x in faq})
            print(f"  增量：沿用 {n-len(todo):,} 筆，重算 {len(todo):,} 筆"
                  f"（新增或內容改過），消失 {gone} 筆")

    # ---- 算 --------------------------------------------------------------
    t0 = time.perf_counter()
    if reuse and not todo:
        dim = old_mat.shape[1]
        mat = np.stack([old_mat[old[x["sid"]][0]] for x in faq]).astype(np.float32)
        print("  沒有任何變動，不用載入模型。")
    else:
        from sentence_transformers import SentenceTransformer
        tm = time.perf_counter()
        model = SentenceTransformer(a.model, device="cpu")
        dim = model.get_sentence_embedding_dimension()
        print(f"  載入 {time.perf_counter()-tm:.0f}s　{dim} 維")
        t0 = time.perf_counter()
        emb = model.encode([texts[i] for i in todo], batch_size=64,
                           normalize_embeddings=True, convert_to_numpy=True,
                           show_progress_bar=True).astype(np.float32)
        if reuse:
            if old_mat.shape[1] != dim:
                sys.exit("錯誤：舊向量的維度與模型不同，請加 --full")
            mat = np.zeros((n, dim), dtype=np.float32)
            keep = [i for i in range(n) if faq[i]["sid"] in old
                    and old[faq[i]["sid"]][1] == hashes[i]]
            for i in keep:
                mat[i] = old_mat[old[faq[i]["sid"]][0]]
            mat[todo] = emb
        else:
            mat = emb
    el = time.perf_counter() - t0

    # 本機用的：float32，查詢時直接拿來算，不損失精度
    np.save(os.path.join(vec, a.tag + ".npy"), mat)

    # 要跟著 git 走的：int8，而且「照 sid 排序」存。
    # faq.json 每天照發布日期重排、新的插在最前面，照那個順序存的話
    # 整個檔案的位元組會全部位移，git 的差異壓縮就失效，每天多 3 MB。
    # 照 sid 存，只有真正改動的那幾列會變 —— 實測每天只成長 0.02 MB。
    sids = [x["sid"] for x in faq]
    order = sorted(range(len(sids)), key=lambda i: sids[i])
    q = np.clip(np.round(mat[order] * 127), -127, 127).astype(np.int8)
    q.tofile(os.path.join(vec, a.tag + ".bin"))

    meta = {
        "tag": a.tag, "model": a.model, "dim": dim, "count": n,
        "query_prefix": qpre, "doc_prefix": dpre, "maxlen": MAXLEN,
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "encode_seconds": round(el, 1),
        "encoded": len(todo), "reused": n - len(todo),
        "mb_float32": round(mat.nbytes / 1e6, 1),
        "mb_int8": round(q.nbytes / 1e6, 2),
        # 比對用。只看筆數擋不住「內容改了但筆數沒變」—— 官網改 FAQ 內文
        # 而不增減筆數是常態，那種情況查詢會默默用到舊向量。
        "faq_fingerprint": faq_fingerprint(faq),
        "order": "sid",
        "sids": [sids[i] for i in order],
    }
    json.dump(meta, open(os.path.join(vec, a.tag + ".json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)

    # 逐筆記錄：.npy 每一列是哪個 sid、那筆當時的內容雜湊。只留在本機（vectors/ 已 gitignore），
    # 網站不需要，也不該讓它每天多 150 KB。
    json.dump({"rows": sids, "hashes": hashes},
              open(os.path.join(vec, a.tag + ".state.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    print(f"  {n:,} 筆完成（重算 {len(todo):,}、沿用 {n-len(todo):,}）{el:.0f}s")
    print(f"  {a.tag}.npy  {mat.nbytes/1e6:.1f} MB（本機查詢用）")
    print(f"  {a.tag}.bin  {q.nbytes/1e6:.2f} MB（int8，照 sid 排序，給網站用）")
    print(f"  {a.tag}.state.json  逐筆記錄（本機增量用）")


if __name__ == "__main__":
    main()
