@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FAQ工作台.lnk"

rem 用 pythonw 啟動才不會留一個黑色主控台視窗在工作列
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath=(Get-Command '%PYW%').Source;" ^
  "$s.Arguments='\"%HERE%工作台.py\"';" ^
  "$s.WorkingDirectory='%HERE%';" ^
  "$s.Description='FAQ 爬蟲工作台（開機自動啟動）';" ^
  "$s.Save()"

if exist "%LNK%" (
  echo.
  echo 設定完成。以後開機就會自動啟動工作台，
  echo 不用再點 啟動.bat，直接開 http://127.0.0.1:8765 就好。
  echo.
  echo 現在先幫你啟動一次…
  start "" %PYW% "%HERE%工作台.py"
  timeout /t 2 >nul
  start http://127.0.0.1:8765
) else (
  echo 設定失敗，捷徑沒有建立。
)
pause
