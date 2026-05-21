@echo off
setlocal

cd /d "%~dp0"

set "HOST=127.0.0.1"
set "PORT=8765"
set "URL=http://%HOST%:%PORT%/"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing virtual environment Python:
  echo         "%PYTHON_EXE%"
  echo.
  echo Create and install it first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo [INFO] Starting multi-agent web service...
start "multiagent-web" cmd /k ""%PYTHON_EXE%" tools\multiagent_web.py --host %HOST% --port %PORT%"

echo [INFO] Waiting for service startup...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2"

echo [INFO] Opening GUI: %URL%
start "" "%URL%"

echo [INFO] Done.
exit /b 0
