#!/bin/bash
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null || { echo "這台電腦沒有 python3"; read -r; exit 1; }
python3 -c "import requests" 2>/dev/null || python3 -m pip install requests
echo
echo "=== 補齊：官網有幾筆就抓到幾筆 ==="
python3 crawl.py
echo
echo "資料已更新到 docs/faq.json"
echo "要把結果同步給其他人，用 GitHub Desktop 按 Commit + Push。"
read -r -p "按 Enter 關閉…"
