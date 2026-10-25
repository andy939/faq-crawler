#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FAQ 檢索。三種方式共用這一份 —— 網站按下去跑的、測試腳本量的，是同一段程式。
分成兩支的話，測出來的分數就不代表網站的實際表現。

    keyword   現在站上的做法：空白分詞，每個詞都要是子字串
    bm25      中文字元 bigram 的 BM25，不需要任何模型
    semantic  向量檢索，需要先用「建立向量.py」算好

中文沒有空白，所以 BM25 不能照英文那樣用空白斷詞。這裡用字元 bigram
（「垃圾車」→ 垃圾、圾車），不需要字典，也不會因為斷詞錯誤整個漏掉。
"""

import json
import math
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
VEC = os.path.join(HERE, "vectors")

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_ASCII = re.compile(r"[a-z0-9]+")


def tokenize(s):
    """英數字照詞切，中文切成字元 bigram。單字的中文詞保留原字。"""
    s = (s or "").lower()
    out = _ASCII.findall(s)
    for run in _CJK.findall(s):
        out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return out


def doc_text(x):
    """給檢索看的文字。標題最重要，機關幫忙消歧義（同名問答分屬不同局處），
    答案補語境。"""
    return f"{x.get('title') or ''}。{x.get('dept') or ''}。{x.get('answer') or ''}"


class Index:
    """載入一次、重複查。工作台是常駐的，不要每次查詢都重建。"""

    def __init__(self, faq_path=None):
        self.faq = json.load(open(faq_path or os.path.join(DOCS, "faq.json"),
                                  encoding="utf-8"))
        self.text = [doc_text(x) for x in self.faq]
        self.hay = [t.lower() for t in self.text]
        self._bm25 = None
        self._vec = {}
        self._fp = None

    def fingerprint(self):
        """這份語料的指紋，跟 建立向量.py 算法必須一致，否則永遠對不起來。"""
        if self._fp is None:
            import hashlib
            h = hashlib.sha256()
            for x in sorted(self.faq, key=lambda r: r["sid"]):
                h.update(json.dumps(
                    [x["sid"], x.get("title") or "", x.get("dept") or "",
                     x.get("answer") or ""],
                    ensure_ascii=False).encode("utf-8"))
            self._fp = h.hexdigest()[:16]
        return self._fp

    # ---- keyword：現在站上的做法 ---------------------------------------
    def keyword(self, q, k=20):
        ts = [t for t in (q or "").lower().split() if t]
        if not ts:
            return []
        out = []
        for i, h in enumerate(self.hay):
            if all(t in h for t in ts):
                out.append((i, 1.0))
                if len(out) >= k:
                    break
        return out

    # ---- BM25 ------------------------------------------------------------
    def _build_bm25(self):
        df, inv, dl = defaultdict(int), defaultdict(list), []
        for i, h in enumerate(self.hay):
            tf = defaultdict(int)
            for w in tokenize(h):
                tf[w] += 1
            dl.append(sum(tf.values()))
            for w, f in tf.items():
                df[w] += 1
                inv[w].append((i, f))
        n = len(self.hay)
        self._bm25 = {
            "inv": inv, "dl": dl, "avgdl": (sum(dl) / n) if n else 1.0,
            "idf": {w: math.log(1 + (n - c + 0.5) / (c + 0.5))
                    for w, c in df.items()},
        }

    def bm25(self, q, k=20, k1=1.5, b=0.75):
        if self._bm25 is None:
            self._build_bm25()
        ix = self._bm25
        sc = defaultdict(float)
        for w in set(tokenize(q)):
            post = ix["inv"].get(w)
            if not post:
                continue
            iw = ix["idf"][w]
            for i, f in post:
                sc[i] += iw * f * (k1 + 1) / (
                    f + k1 * (1 - b + b * ix["dl"][i] / ix["avgdl"]))
        return sorted(sc.items(), key=lambda kv: -kv[1])[:k]

    # ---- 語意 ------------------------------------------------------------
    def _load_vec(self, tag):
        """向量和查詢必須是同一個模型算的，所以模型名與前綴都存在 meta 裡，
        不在這裡猜。"""
        if tag in self._vec:
            return self._vec[tag]
        import numpy as np
        meta = json.load(open(os.path.join(VEC, tag + ".json"), encoding="utf-8"))
        mat = np.load(os.path.join(VEC, tag + ".npy"))
        if len(mat) != len(self.faq):
            raise RuntimeError(
                f"{tag} 的向量有 {len(mat)} 筆，faq.json 有 {len(self.faq)} 筆。"
                f"資料更新過了（向量建於 {meta.get('built_at','?')}），"
                "請重跑 建立向量.py")
        # 只比筆數擋不住「內容改了但筆數沒變」—— 官網改 FAQ 內文而不增減
        # 筆數是常態，那種情況查詢會默默用到舊向量，而且完全不會報錯。
        want = meta.get("faq_fingerprint")
        if want and want != self.fingerprint():
            raise RuntimeError(
                f"{tag} 的向量是用舊的 faq.json 算的"
                f"（建於 {meta.get('built_at','?')}，指紋 {want}；"
                f"目前資料的指紋是 {self.fingerprint()}）。請重跑 建立向量.py")
        from sentence_transformers import SentenceTransformer
        self._vec[tag] = (SentenceTransformer(meta["model"], device="cpu"),
                          mat, meta)
        return self._vec[tag]

    def semantic(self, q, tag, k=20):
        import numpy as np
        model, mat, meta = self._load_vec(tag)
        v = model.encode([meta.get("query_prefix", "") + q],
                         normalize_embeddings=True,
                         convert_to_numpy=True)[0].astype(mat.dtype)
        sc = mat @ v
        top = np.argpartition(-sc, min(k, len(sc) - 1))[:k]
        top = top[np.argsort(-sc[top])]
        return [(int(i), float(sc[i])) for i in top]

    # ---- 混合（RRF）------------------------------------------------------
    def hybrid(self, q, tag, k=20, depth=50, rrf_k=60):
        """BM25 和語意各出一份排名，用 RRF（Reciprocal Rank Fusion）合併。

        為什麼不直接把分數相加：兩邊的量級差太多。BM25 是 0~20 幾的加總，
        餘弦相似度永遠擠在 0.75~0.92，相加等於 BM25 說了算。

        RRF 只看「第幾名」不看分數：每一路貢獻 1/(60+名次)，所以
        第 1 名 0.0164、第 2 名 0.0161……兩路都排前面的會浮上來，
        只有一路排前面的也還有機會，是標準做法。

        depth 取 50：兩邊各看前 50 名。取太淺會漏掉「一路沒中、另一路
        排第 30 名」那種可以救回來的題目。
        """
        pools = (self.bm25(q, depth), self.semantic(q, tag, depth))
        fused = {}
        for pool in pools:
            for rank, (i, _) in enumerate(pool, 1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + rank)
        return sorted(fused.items(), key=lambda kv: -kv[1])[:k]

    # ---- 統一入口 --------------------------------------------------------
    def search(self, q, mode="bm25", tag=None, k=20):
        if mode == "keyword":
            hits = self.keyword(q, k)
        elif mode == "bm25":
            hits = self.bm25(q, k)
        elif mode == "semantic":
            hits = self.semantic(q, tag, k)
        elif mode == "hybrid":
            hits = self.hybrid(q, tag, k)
        else:
            raise ValueError(f"沒有這種檢索方式：{mode}")
        return [dict(self.faq[i], _score=round(s, 4)) for i, s in hits]
