@echo off
chcp 65001 >nul
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FAQ工作台.lnk"
if exist "%LNK%" (
  del "%LNK%"
  echo 已取消開機自動啟動。
  echo 之後要用工作台，雙擊 啟動.bat 即可。
) else (
  echo 本來就沒有設定開機自動啟動。
)
pause
