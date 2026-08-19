@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0" || exit /b 1
title 91Fetch
where py >nul 2>nul
if not errorlevel 1 py -3 launcher.py
if not errorlevel 1 goto :done
where python >nul 2>nul
if not errorlevel 1 python launcher.py
if not errorlevel 1 goto :done
echo Python 3.11 or newer is required.
set "EXIT_CODE=1"
:done
if not defined EXIT_CODE set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
