[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
Push-Location $root
try {
    & $python -m pip check
    if ($LASTEXITCODE) { throw 'Dependency check failed.' }
    & $python tools/security_check.py
    if ($LASTEXITCODE) { throw 'Publication boundary failed.' }
    & $python tools/validate_automation.py
    if ($LASTEXITCODE) { throw 'Workflow contracts failed.' }
    & $python -m ruff check central tests tools
    if ($LASTEXITCODE) { throw 'Ruff failed.' }
    & $python tools/run_bandit.py
    if ($LASTEXITCODE) { throw 'Bandit failed.' }
    & $python -m pytest -q
    if ($LASTEXITCODE) { throw 'Pytest failed.' }
} finally {
    Pop-Location
}
