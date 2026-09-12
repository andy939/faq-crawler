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
python 工作台.py
pause
