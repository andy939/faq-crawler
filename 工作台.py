#!/usr/bin/env python3
"""
本機工作台 —— 把 docs/ 這個網頁跑起來，並且讓網頁上的按鈕可以真的去抓資料。

    python 工作台.py          # 開 http://127.0.0.1:8765

跟 GitHub Pages 上那頁是同一個 index.html。差別只在：
放在 GitHub 上是唯讀的監控頁（靜態網頁沒有後端，按不動），
在這裡打開就會多出一塊「本機控制台」，可以選模式、按下去抓、看即時進度。

抓取本身是開一個子行程去跑 crawl.py，所以行為跟你雙擊 更新.bat
或 GitHub Actions 上跑的完全一樣，不會有第二套邏輯。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
PORT = 8765

MODES = {
    "fill":    ("補齊（官網有幾筆就抓到幾筆）", []),
    "refresh": ("補齊＋複查 300 筆", ["--refresh", "300"]),
    "full":    ("全站重抓（約 50 分鐘）", ["--full"]),
    "check":   ("環境檢查", ["--check"]),
    "dry":     ("試跑，不寫檔", ["--dry-run"]),
}


class Job:
    """同時只跑一個。抓取中再按一次不會疊上去，只會被擋掉。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.proc = None
        self.mode = ""
        self.started = 0.0
        self.code = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, key):
        with self.lock:
            if self.running:
                return False, "已經有一輪在跑了"
            label, args = MODES[key]
            self.lines = [f"$ python crawl.py {' '.join(args)}".rstrip()]
            self.mode, self.started, self.code = label, time.time(), None
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "crawl.py"] + args,
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                # Windows 下不要另外彈出一個主控台視窗
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, daemon=True).start()
        return True, label

    def _pump(self):
        for raw in self.proc.stdout:
            # crawl.py 的進度是用 \r 原地更新的，這裡拆成一行一行
            for line in raw.replace("\r", "\n").split("\n"):
                if line.strip():
                    self.lines.append(line.rstrip())
            del self.lines[:-400]          # 只留最後 400 行
        self.code = self.proc.wait()
        el = time.time() - self.started
        self.lines.append(
            f"—— 結束（{'成功' if self.code == 0 else f'離開碼 {self.code}'}），"
            f"共 {el:.0f} 秒 ——")

    def state(self, since=0):
        return {
            "running": self.running, "mode": self.mode, "code": self.code,
            "elapsed": round(time.time() - self.started, 1) if self.started else 0,
            "total": len(self.lines), "lines": self.lines[since:],
        }


JOB = Job()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DOCS, **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/ping"):
            # 網頁靠這支判斷「我是不是跑在本機工作台上」，
            # 在 GitHub Pages 上這支會 404，網頁就只顯示唯讀的監控。
            return self._json({"local": True,
                               "modes": [{"key": k, "label": v[0]}
                                         for k, v in MODES.items()]})
        if self.path.startswith("/api/log"):
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    pass
            return self._json(JOB.state(since))
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/run"):
            return self.send_error(404)
        key = self.path.split("mode=")[1].split("&")[0] if "mode=" in self.path else "fill"
        if key not in MODES:
            return self._json({"ok": False, "msg": "不認得的模式"}, 400)
        ok, msg = JOB.start(key)
        return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)

    def end_headers(self):
        # 資料檔每次抓完就變，不要讓瀏覽器拿舊的
        if self.path.endswith((".json", ".csv", ".html")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *a):
        pass                                # 不要把每個請求都印出來洗版


def main():
    if not os.path.exists(os.path.join(DOCS, "index.html")):
        sys.exit(f"找不到 {DOCS}\\index.html")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"本機工作台：{url}")
    print("網頁上的「本機控制台」可以直接按下去抓資料。")
    print("關掉這個視窗（或按 Ctrl-C）就停止。\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()
