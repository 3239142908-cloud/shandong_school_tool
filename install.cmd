@echo off
rem One-click dependency install. Creates .\venv if missing.
setlocal enabledelayedexpansion
chcp 65001 >nul
set "HERE=%~dp0"
cd /d "%HERE%"

rem Prefer Python 3.12 from the py launcher (best wheel compatibility),
rem then any py -3, then whatever python is on PATH.
set "BASE="
py -3.12 --version >nul 2>&1 && set "BASE=py -3.12"
if not defined BASE py -3 --version >nul 2>&1 && set "BASE=py -3"
if not defined BASE python --version >nul 2>&1 && set "BASE=python"
if not defined BASE (
  echo [ERROR] No Python found. Install Python 3.10-3.12 first: https://www.python.org/downloads/
  pause
  exit /b 1
)
echo [0/3] Using: !BASE!
for /f "delims=" %%v in ('!BASE! --version 2^>^&1') do echo        %%v

if not exist "%HERE%venv\Scripts\python.exe" (
  echo [1/3] Creating venv ...
  !BASE! -m venv venv
  if errorlevel 1 (
    echo [ERROR] venv creation failed.
    pause
    exit /b 1
  )
) else (
  echo [1/3] venv already exists
)

set "PY=%HERE%venv\Scripts\python.exe"
echo [2/3] Upgrading pip ...
"%PY%" -m pip install --upgrade pip --quiet

echo [3/3] Installing dependencies ...
"%PY%" -m pip install requests pycryptodome numpy opencv-python-headless --quiet
if errorlevel 1 (
  echo [ERROR] install failed. Run manually to see the error:
  echo   "%PY%" -m pip install requests pycryptodome numpy opencv-python-headless
  pause
  exit /b 1
)

echo.
echo Done. Now run:  run.cmd --list
pause
