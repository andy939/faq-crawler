#!/usr/bin/env python3
"""
把抓好的資料同步到兩個 GitHub 網站。

    python 同步網站.py              # 兩個站都同步
    python 同步網站.py --only ms    # 只同步秘密客
    python 同步網站.py --dry-run    # 只轉檔、比對差異，不 commit 不 push

做的事：轉成各站的格式 → 覆蓋該 repo 的 data/ → 有變動才 commit → push。

    ms      andy939/1999-mystery-shopper   https://andy939.github.io/1999-mystery-shopper/desktop.html
    search  andy939/taipei-faq-search      https://andy939.github.io/taipei-faq-search/

沒有變動就什麼都不做，不會產生空 commit。

GitHub Actions 也會跑這支（crawl.yml 的「同步到兩個網站」）：那台機器上沒有
D:\ai_work\1150907秘密客，秘密客會自動 clone 到 _repos/ 底下。
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 秘密客在這台電腦上有另一個工作階段在用的資料夾，有就沿用；沒有（雲端、別台電腦）就 clone 一份
MS_HOME = r"D:\ai_work\1150907秘密客\_site_ms"

SITES = {
    "ms": {
        "name": "1999 秘密客抽測台",
        "repo": "https://github.com/andy939/1999-mystery-shopper.git",
        "local": MS_HOME if os.path.isdir(MS_HOME)
                 else os.path.join(HERE, "_repos", "1999-mystery-shopper"),
        "out": os.path.join(HERE, "_site_ms_out", "data"),
        "url": "https://andy939.github.io/1999-mystery-shopper/desktop.html",
    },
    "search": {
        "name": "臺北市 FAQ 搜尋站",
        "repo": "https://github.com/andy939/taipei-faq-search.git",
        "local": os.path.join(HERE, "_repos", "taipei-faq-search"),
        "out": os.path.join(HERE, "_site_faq_out", "data"),
        "url": "https://andy939.github.io/taipei-faq-search/",
    },
}


def run(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise SystemExit(f"指令失敗：{' '.join(args)}\n{p.stdout}{p.stderr}")
    return p.stdout.strip()


def ensure_repo(site):
    """本機沒有就 clone 一份。已經有的話先 pull，免得推不上去。"""
    d = site["local"]
    if not os.path.isdir(os.path.join(d, ".git")):
        os.makedirs(os.path.dirname(d), exist_ok=True)
        print(f"  clone {site['repo']} …")
        run(["git", "clone", "--depth", "50", site["repo"], d])
    else:
        run(["git", "-C", d, "pull", "--rebase", "--autostash"], check=False)
    return d


def sync(key, dry=False):
    site = SITES[key]
    print(f"\n=== {site['name']} ===")

    # 1. 先把網站 repo 拿到最新 —— 點閱數、分類這些我們的爬蟲沒有的欄位，
    #    要從網站現有的 data/ 接著用。以前先轉檔才 clone，在雲端第一次跑時
    #    repo 還不存在，點閱數會全部變成 0。
    repo = ensure_repo(site)
    dst = os.path.join(repo, "data")

    # 2. 轉檔
    r = subprocess.run([sys.executable, "匯出到網站.py", "--target", key,
                        "--old", dst],
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print("  " + r.stdout.strip().replace("\n", "\n  "))
    if r.returncode:
        print("  轉檔失敗：" + r.stderr[:300])
        return False

    # 3. 蓋進 repo 的 data/
    if os.path.isdir(dst):
        shutil.rmtree(dst)          # 舊的分片數可能跟新的不一樣，整個換掉
    shutil.copytree(site["out"], dst)

    # 4. 有變動才 commit
    changed = run(["git", "-C", repo, "status", "--porcelain", "data"])
    if not changed:
        print("  資料沒有變動，不用同步")
        return True
    n = len(changed.splitlines())
    print(f"  有 {n} 個檔案變動")
    if dry:
        print("  （dry-run，沒有 commit／push）")
        return True

    import time
    run(["git", "-C", repo, "add", "data"])
    run(["git", "-C", repo, "commit", "-m",
         f"資料更新 {time.strftime('%Y-%m-%d %H:%M')}（來源：faq-crawler）"])
    run(["git", "-C", repo, "push"])
    print(f"  已推上去 → {site['url']}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=tuple(SITES), help="只同步其中一個")
    ap.add_argument("--dry-run", action="store_true", help="只轉檔比對，不推")
    a = ap.parse_args()
    keys = [a.only] if a.only else list(SITES)
    ok = all(sync(k, a.dry_run) for k in keys)
    print("\n完成。" if ok else "\n有站台沒同步成功，看上面的訊息。")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
