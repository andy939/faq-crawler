#!/usr/bin/env python3
"""
本機工作台 —— 把 docs/ 這個網頁跑起來，並且讓網頁上的按鈕可以真的去抓資料。

    python 工作台.py          # 開 http://127.0.0.1:8765

跟 GitHub Pages 上那頁是同一個 index.html。差別只在：
放在 GitHub 上是唯讀的監控頁（靜態網頁沒有後端，按不動），
在這裡打開就會多出整塊控制台 —— 選抓法、筆數、日期、限速，按下去抓，
八個指標即時跳動，可以中途停止。

抓取本身是開一個子行程去跑 crawl.py，所以行為跟你雙擊 更新.bat
或 GitHub Actions 上跑的完全一樣，不會有第二套邏輯。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import crawl as C

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
PORT = 8765

# 網頁上那排工具按鈕。key 是網頁送來的名字，值是要跑的命令列。
TOOLS = {
    "check":  ("環境檢查", [sys.executable, "-u", "crawl.py", "--check"]),
    "fill":   ("補齊（官網幾筆就抓幾筆）", [sys.executable, "-u", "crawl.py"]),
    "full":   ("全站重抓", [sys.executable, "-u", "crawl.py", "--full"]),
    "report": ("產生分析報表", [sys.executable, "-u", "report.py"]),
    "excel":  ("產生 Excel", [sys.executable, "-u", "to_excel.py"]),
}


class Job:
    """同時只跑一個。抓取中再按一次不會疊上去，只會被擋掉。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.metrics = {}
        self.proc = None
        self.label = ""
        self.cmd = ""
        self.started = 0.0
        self.code = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, label, args):
        with self.lock:
            if self.running:
                return False, "已經有一輪在跑了"
            if os.path.exists(C.STOP):
                os.remove(C.STOP)
            self.cmd = " ".join(a for a in args[2:]) or "（預設）"
            self.lines = [f"$ python {' '.join(args[2:])}".rstrip()]
            self.metrics = {}
            self.label, self.started, self.code = label, time.time(), None
            env = dict(os.environ, CRAWL_JSON="1", PYTHONIOENCODING="utf-8")
            self.proc = subprocess.Popen(
                args, cwd=HERE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env,
                # Windows 下不要另外彈出一個主控台視窗
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, daemon=True).start()
        return True, label

    def stop(self):
        """建一個旗標檔，crawl.py 每次請求前會看。這樣是「好好地停」——
        已經抓到的會存檔，不像直接砍掉行程什麼都不剩。"""
        if not self.running:
            return False
        open(C.STOP, "w").close()
        self.lines.append("—— 已要求停止，等這一筆結束後收尾… ——")
        return True

    def _pump(self):
        for raw in self.proc.stdout:
            if raw.startswith("@@"):          # 機器讀的即時指標
                try:
                    self.metrics.update(json.loads(raw[2:]))
                except ValueError:
                    pass
                continue
            # crawl.py 的進度是用 \r 原地更新的，這裡拆成一行一行
            for line in raw.replace("\r", "\n").split("\n"):
                if line.strip():
                    self.lines.append(line.rstrip())
            del self.lines[:-600]             # 只留最後 600 行
        self.code = self.proc.wait()
        el = time.time() - self.started
        self.lines.append(
            f"—— 結束（{'成功' if self.code == 0 else f'離開碼 {self.code}'}），"
            f"共 {el:.0f} 秒 ——")

    def state(self, since=0):
        return {
            "running": self.running, "label": self.label, "cmd": self.cmd,
            "code": self.code, "metrics": self.metrics,
            "elapsed": round(time.time() - self.started, 1) if self.started else 0,
            "total": len(self.lines), "lines": self.lines[since:],
        }


JOB = Job()


def build_args(q):
    """把網頁送來的參數組成 crawl.py 的命令列。"""
    args = [sys.executable, "-u", "crawl.py"]
    mode = q.get("mode", ["fill"])[0]
    if mode == "full":
        args.append("--full")
    elif mode == "refresh":
        args += ["--refresh", str(int(q.get("refresh", ["300"])[0] or 300))]
    elif mode == "check":
        return "環境檢查", args + ["--check"]
    elif mode == "dry":
        args.append("--dry-run")

    method = q.get("method", ["B"])[0]
    if method in C.METHODS:
        args += ["--method", method]
    limit = q.get("limit", [""])[0]
    if limit and limit.isdigit() and int(limit) > 0:
        args += ["--limit", limit]
    for k, flag in (("from", "--from"), ("to", "--to")):
        v = (q.get(k, [""])[0] or "").strip()
        if v:
            args += [flag, v]
    rate = q.get("rate", [""])[0]
    if rate and rate.isdigit():
        args += ["--rate-ms", rate]
    if q.get("force", [""])[0] == "1":
        args.append("--force")

    label = {"fill": "補齊", "full": "全站重抓", "refresh": "補齊＋複查",
             "dry": "試跑"}.get(mode, mode)
    label += f"・抓法 {method}"
    if limit:
        label += f"・{limit} 筆"
    return label, args


def latest_wire():
    """從 exports/ 的逐筆測量檔撈出每一筆的「抓下 byte」，新的蓋舊的。"""
    out = {}
    try:
        import csv
        import glob
        for p in sorted(glob.glob(os.path.join(HERE, "exports", "*.csv"))):
            with open(p, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("sid") and r.get("抓下bytes"):
                        out[r["sid"]] = int(float(r["抓下bytes"]))
    except Exception:
        pass
    return out


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
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        if path == "/api/ping":
            # 網頁靠這支判斷「我是不是跑在本機工作台上」，
            # 在 GitHub Pages 上這支會 404，網頁就只顯示唯讀的監控。
            return self._json({
                "local": True,
                "methods": [{"key": k, "label": v[0], "desc": v[1],
                             "workers": v[2].get("workers", 1)}
                            for k, v in C.METHODS.items()],
                "tools": [{"key": k, "label": v[0]} for k, v in TOOLS.items()],
                "rate_default": C.RATE_MS,
            })
        if path == "/api/log":
            since = int(q.get("since", ["0"])[0] or 0)
            return self._json(JOB.state(since))
        if path == "/api/samples":
            # 逐筆的「抓下 byte」只存在 exports/ 的測量檔裡（那是每次抓取的
            # 量測值，不是問答本身的屬性，所以沒進 faq.json）。
            # 網頁那張逐筆表要顯示它，就從最近的測量檔撈出來，新的蓋舊的。
            return self._json({"wire": latest_wire()})
        return super().do_GET()

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        if path == "/api/stop":
            return self._json({"ok": JOB.stop()})
        if path == "/api/tool":
            key = q.get("key", [""])[0]
            if key not in TOOLS:
                return self._json({"ok": False, "msg": "不認得的工具"}, 400)
            label, args = TOOLS[key]
            ok, msg = JOB.start(label, args)
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
        if path == "/api/run":
            label, args = build_args(q)
            ok, msg = JOB.start(label, args)
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)
        return self.send_error(404)

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
    print("網頁上的控制台可以直接按下去抓資料，也可以中途停止。")
    print("關掉這個視窗（或按 Ctrl-C）就停止。\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()
