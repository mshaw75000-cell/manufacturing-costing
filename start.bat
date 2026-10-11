@echo off
rem Double-click to run Manufacturing Costing on this PC (http://localhost:8000).
rem First run creates a Python environment, installs packages and loads the trial data.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" on the first screen of the installer.
  pause
  exit /b 1
)
if not exist .venv (
  echo Setting up for the first time, this takes a minute...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist mfgcost.db (
  echo Loading trial data...
  python -m app.seed --trial
)
echo.
echo Manufacturing Costing is running at http://localhost:8000
echo Leave this window open while you use it. Close it to stop.
echo.
start "" cmd /c "timeout /t 3 >nul & start http://localhost:8000"
python -m uvicorn app.main:app --port 8000
pause
