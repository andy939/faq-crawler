#!/bin/bash
cd "$(dirname "$0")/docs" || exit 1
echo "網頁工作台：http://127.0.0.1:8765"
echo "按 Ctrl-C 停止。"
(sleep 1; open http://127.0.0.1:8765) &
python3 -m http.server 8765 --bind 127.0.0.1
