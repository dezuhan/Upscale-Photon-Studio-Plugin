@echo off
setlocal
cd /d "%~dp0..\service"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  pause
  exit /b 1
)

echo Photon Upscale service - http://127.0.0.1:8765
echo Keep this window open while the plugin is in use. Press Ctrl+C to stop.
echo.

python server.py %*
if errorlevel 1 pause
