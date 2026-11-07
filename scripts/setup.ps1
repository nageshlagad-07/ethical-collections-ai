# One-time setup on Windows PowerShell. Run from the repository root.
$ErrorActionPreference = "Stop"
if (-not (Test-Path .venv)) { python -m venv .venv }
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
# Build the SQLite database from the shipped CSV dataset and run the engines (reconcile, prioritize, detect, validate)
python -m collections_ai.cli init-db --data data/full
python -m collections_ai.cli run-engines
Write-Host "Setup complete. Start the API with scripts\run_api.ps1 and the dashboard with scripts\run_dashboard.ps1"
