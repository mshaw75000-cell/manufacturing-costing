@echo off
rem Deletes the local database and simulated BC so the next start reloads fresh trial data.
cd /d "%~dp0"
del /q mfgcost.db bc_mock.json 2>nul
echo Data cleared. Run start.bat to load fresh trial data.
pause
