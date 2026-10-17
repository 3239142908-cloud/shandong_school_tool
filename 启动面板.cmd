@echo off
rem Launch the GUI panel. Falls back to a visible console if the GUI dies.
chcp 65001 >nul
title ShanDong School 8.7.7
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"
set "LOG=%HERE%panel_launch.log"

set "PY="
if exist "%HERE%venv\Scripts\python.exe" set "PY=%HERE%venv\Scripts\python.exe"
if not defined PY if defined SHANDONG_PYTHON set "PY=%SHANDONG_PYTHON%"
if not defined PY set "PY=python"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

rem --- dependency gate ---
"%PY%" -c "import Crypto, requests, numpy, cv2" >nul 2>&1
if errorlevel 1 goto :nodeps

set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"

echo ==== %date% %time% ==== > "%LOG%"
echo PY  = %PY%>> "%LOG%"
echo PYW = %PYW%>> "%LOG%"
echo CWD = %CD%>> "%LOG%"

start "" "%PYW%" "%HERE%panel.py"

rem --- 2 秒后确认进程还在；不在就把错误打出来 ---
ping -n 3 127.0.0.1 >nul 2>&1
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if not errorlevel 1 exit /b 0
tasklist /fi "imagename eq python.exe" 2>nul | find /i "python.exe" >nul
if not errorlevel 1 exit /b 0

echo.
echo   ============================================================
echo    GUI failed to start.
echo   ============================================================
echo.
if exist "%HERE%panel_error.log" (
  echo   --- panel_error.log ---
  type "%HERE%panel_error.log"
  echo   ----------------------
) else (
  echo   No panel_error.log. Trying console mode to show the error:
  echo.
  "%PY%" "%HERE%panel.py"
)
echo.
pause
exit /b 1

:nodeps
echo.
echo   ============================================================
echo    Missing dependencies or no Python found.
echo   ============================================================
echo.
echo    Run install.cmd first ^(double-click it^), then run this again.
echo.
echo    Interpreter tried: %PY%
echo    Checked modules : Crypto requests numpy cv2
echo.
pause
exit /b 1
