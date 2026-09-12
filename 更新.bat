@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 這台電腦沒有 Python。
  echo 到 https://www.python.org/downloads/ 下載安裝，
  echo 安裝時記得勾選「Add python.exe to PATH」，然後再跑一次。
  pause & exit /b 1
)
python -c "import requests" 2>nul || python -m pip install requests
echo.
echo === 補齊：官網有幾筆就抓到幾筆 ===
python crawl.py
echo.
echo 資料已更新到 docs\faq.json
echo 要把結果同步給其他人，用 GitHub Desktop 按 Commit + Push。
pause
