$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
Write-Host "Open http://127.0.0.1:8000. Fixture data and rule-based replay are enabled."
& .\.venv\Scripts\python.exe -m travel_agent.cli serve
