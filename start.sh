#!/bin/sh
# Run Manufacturing Costing on this Mac/Linux machine (http://localhost:8000).
# First run creates a Python environment, installs packages and loads the trial data.
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null || { echo "Install Python 3.11+ from https://www.python.org/downloads/"; exit 1; }
[ -d .venv ] || { echo "Setting up for the first time..."; python3 -m venv .venv; }
. .venv/bin/activate
python -m pip install -q -r requirements.txt
[ -f mfgcost.db ] || { echo "Loading trial data..."; python -m app.seed --trial; }
echo "Running at http://localhost:8000 (Ctrl+C to stop)"
( sleep 3; command -v open >/dev/null && open http://localhost:8000 || xdg-open http://localhost:8000 ) >/dev/null 2>&1 &
exec python -m uvicorn app.main:app --port 8000
