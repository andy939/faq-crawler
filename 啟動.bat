@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo 臺北市政府常見問答 . 爬蟲工作台
echo ================================
echo.
python -c "import requests" 2>nul || (
  echo 第一次啟動，正在安裝需要的套件...
  python -m pip install --quiet requests openpyxl
)
echo 啟動中... 瀏覽器會自動打開 http://127.0.0.1:8765
echo 要結束的話，關掉這個視窗就好。
echo.
python app.py
pause
