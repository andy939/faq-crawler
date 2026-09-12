@echo off
chcp 65001 >nul
cd /d "%~dp0docs"
echo 網頁工作台：http://127.0.0.1:8765
echo 關掉這個視窗就停止。
start http://127.0.0.1:8765
python -m http.server 8765 --bind 127.0.0.1
