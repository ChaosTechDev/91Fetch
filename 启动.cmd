@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title 91Fetch
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  echo 91Fetch 需要 Python 3.11 或更高版本。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在创建 91Fetch 本地环境...
  %PYTHON_CMD% -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
  ".venv\Scripts\python.exe" -m pip install -e . || goto :error
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -e . || goto :error

if not exist "site.json" copy /y "site.example.json" "site.json" >nul
echo 正在启动 91Fetch，网页将自动打开...
".venv\Scripts\python.exe" -m viewkey_batch.web
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%

:error
echo 启动失败，请检查网络连接和 Python 安装。
pause
exit /b 1
