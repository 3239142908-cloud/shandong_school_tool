@echo off
rem Double-click entry point. Calls menu.py (interactive console).
rem Window stays open on exit so errors are readable.
chcp 65001 >nul
title ShanDong School 8.7.7
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

rem --- resolve python: project venv > SHANDONG_PYTHON > PATH ---
set "PY="
if exist "%HERE%venv\Scripts\python.exe" set "PY=%HERE%venv\Scripts\python.exe"
if not defined PY if defined SHANDONG_PYTHON set "PY=%SHANDONG_PYTHON%"
if not defined PY set "PY=python"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

rem --- dependency gate: fail early with an actionable message ---
"%PY%" -c "import Crypto, requests, numpy, cv2" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   ============================================================
  echo    Missing dependencies ^(or no Python^).
  echo   ============================================================
  echo.
  echo    Run install.cmd first ^(double-click it^), then run this again.
  echo.
  echo    Current interpreter: %PY%
  echo    Required: requests pycryptodome numpy opencv-python-headless
  echo.
  pause
  exit /b 1
)

rem --- fetch vendor tls backend path if present ---
if exist "%HERE%vendor" set "PYTHONPATH=%HERE%vendor;%PYTHONPATH%"

"%PY%" "%HERE%menu.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [exit code: %RC%]
echo   Press any key to close...
pause >nul
endlocal & exit /b %RC%
