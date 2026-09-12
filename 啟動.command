#!/bin/bash
# 雙擊這個檔案就會啟動工作台並開啟瀏覽器。
cd "$(dirname "$0")"
echo "臺北市政府常見問答 · 爬蟲工作台"
echo "================================"
echo

# 找得到哪個 python 就用哪個
PY=""
for c in python3 python /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  command -v "$c" >/dev/null 2>&1 && PY="$c" && break
done
if [ -z "$PY" ]; then
  echo "找不到 Python。請先安裝：https://www.python.org/downloads/"
  echo; read -p "按 Enter 關閉..."; exit 1
fi

# 缺套件就自動裝
if ! "$PY" -c "import requests" 2>/dev/null; then
  echo "第一次啟動，正在安裝需要的套件（約 30 秒）..."
  "$PY" -m pip install --quiet requests openpyxl
  echo "安裝完成"; echo
fi

echo "啟動中… 瀏覽器會自動打開 http://127.0.0.1:8765"
echo "要結束的話，關掉這個視窗就好。"
echo
"$PY" app.py
echo
read -p "已結束。按 Enter 關閉視窗..."
