@echo off
rem Double-click to start the Software Factory. It opens the dashboard as an app window.
rem Options (in a terminal, after the file name): --browser  --phone  --awake  --no-open
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo The Software Factory needs "uv". Install it from https://docs.astral.sh/uv/getting-started/installation/ and double-click this again.
  pause
  exit /b 1
)
uv run --no-project python scripts\sf.py start %*
if errorlevel 1 (
  echo.
  echo Something went wrong ^(see above^).
  pause
)
