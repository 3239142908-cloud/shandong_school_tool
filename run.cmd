@echo off
rem ShanDong School 8.7.7 launcher
rem Usage:
rem   run.cmd --list
rem   run.cmd run_cli -t free -d 2000 --lat 30.0 --lng 120.0
rem Python resolution order: .\venv\Scripts\python.exe -> SHANDONG_PYTHON -> python on PATH
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "PY="
if exist "%HERE%venv\Scripts\python.exe" set "PY=%HERE%venv\Scripts\python.exe"
if not defined PY if defined SHANDONG_PYTHON set "PY=%SHANDONG_PYTHON%"
if not defined PY set "PY=python"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
"%PY%" "%HERE%run.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
