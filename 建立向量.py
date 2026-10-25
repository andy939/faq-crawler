#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/faq.json 用指定的模型算成向量，存到 vectors/<代號>.npy。

    python 建立向量.py --model intfloat/multilingual-e5-small --tag e5-small
    python 建立向量.py --model BAAI/bge-m3                     --tag bge-m3
    python 建立向量.py --list        # 看已經算過哪些

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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
VEC = os.path.join(HERE, "vectors")

# 文件截斷長度。答案長度中位數才 140 字，1200 已經涵蓋絕大多數；
# 留這個上限是因為少數法規類答案有上萬字，整篇餵進去又慢又會稀釋重點。
MAXLEN = 1200


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="Hugging Face 模型名稱")
    ap.add_argument("--tag", help="代號，用來當檔名與 /api/search 的參數")
    ap.add_argument("--list", action="store_true", help="列出已經算過的")
    a = ap.parse_args()

    os.makedirs(VEC, exist_ok=True)
    if a.list or not a.model:
        rows = [f for f in os.listdir(VEC) if f.endswith(".json")]
        if not rows:
            print("還沒有算過任何向量。")
        for f in sorted(rows):
            m = json.load(open(os.path.join(VEC, f), encoding="utf-8"))
            print(f"  {m['tag']:16} {m['model']:42} {m['dim']:>5} 維　"
                  f"{m['count']:,} 筆　{m['built_at']}")
        return
    if not a.tag:
        ap.error("要給 --tag")

    import numpy as np
    from sentence_transformers import SentenceTransformer
    import 檢索 as R

    faq = json.load(open(os.path.join(DOCS, "faq.json"), encoding="utf-8"))
    qpre, dpre = prefixes(a.model)
    print(f"模型 {a.model}")
    print(f"  查詢前綴 {qpre!r}　文件前綴 {dpre!r}")

    t0 = time.perf_counter()
    model = SentenceTransformer(a.model, device="cpu")
    dim = model.get_sentence_embedding_dimension()
    print(f"  載入 {time.perf_counter()-t0:.0f}s　{dim} 維")

    texts = [dpre + R.doc_text(x).replace("\n", " ")[:MAXLEN] for x in faq]
    t0 = time.perf_counter()
    mat = model.encode(texts, batch_size=64, normalize_embeddings=True,
                       convert_to_numpy=True, show_progress_bar=True
                       ).astype(np.float32)
    el = time.perf_counter() - t0

    # 本機用的：float32，查詢時直接拿來算，不損失精度
    np.save(os.path.join(VEC, a.tag + ".npy"), mat)

    # 要跟著 git 走的：int8，而且「照 sid 排序」存。
    # faq.json 每天照發布日期重排、新的插在最前面，照那個順序存的話
    # 整個檔案的位元組會全部位移，git 的差異壓縮就失效，每天多 3 MB。
    # 照 sid 存，只有真正改動的那幾列會變 —— 實測每天只成長 0.02 MB。
    sids = [x["sid"] for x in faq]
    order = sorted(range(len(sids)), key=lambda i: sids[i])
    q = np.clip(np.round(mat[order] * 127), -127, 127).astype(np.int8)
    q.tofile(os.path.join(VEC, a.tag + ".bin"))

    meta = {
        "tag": a.tag, "model": a.model, "dim": dim, "count": len(faq),
        "query_prefix": qpre, "doc_prefix": dpre, "maxlen": MAXLEN,
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "encode_seconds": round(el, 1),
        "mb_float32": round(mat.nbytes / 1e6, 1),
        "mb_int8": round(q.nbytes / 1e6, 2),
        # 比對用。只看筆數擋不住「內容改了但筆數沒變」—— 官網改 FAQ 內文
        # 而不增減筆數是常態，那種情況查詢會默默用到舊向量。
        "faq_fingerprint": faq_fingerprint(faq),
        "order": "sid",
        "sids": [sids[i] for i in order],
    }
    json.dump(meta, open(os.path.join(VEC, a.tag + ".json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  {len(faq):,} 筆編碼完成 {el:.0f}s")
    print(f"  vectors/{a.tag}.npy  {mat.nbytes/1e6:.1f} MB（本機查詢用）")
    print(f"  vectors/{a.tag}.bin  {q.nbytes/1e6:.2f} MB（int8，照 sid 排序，給網站用）")


if __name__ == "__main__":
    main()
