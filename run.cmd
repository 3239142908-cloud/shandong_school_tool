@echo off
rem ShanDong School 8.7.7 launcher.
rem Usage: run.cmd --list
rem        run.cmd login_cli --help
rem        run.cmd run_cli -t free -d 2000 --lat 36.67 --lng 117.05
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
if not defined SHANDONG_PYTHON set "SHANDONG_PYTHON=<PYTHON_EXE>"
if not exist "%SHANDONG_PYTHON%" set "SHANDONG_PYTHON=python"
set "PYTHONIOENCODING=utf-8"
rem 禁止生成 __pycache__：在只读或受限目录里运行时不会因写缓存报权限错误
set "PYTHONDONTWRITEBYTECODE=1"
"%SHANDONG_PYTHON%" "%HERE%run.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%