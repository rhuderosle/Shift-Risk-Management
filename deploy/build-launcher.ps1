# Builds StartShiftRisk.exe from launcher.py.
#
# The .exe itself is not committed to git (it's a compiled binary and this
# repo is public) - run this once after cloning to produce a local launcher.
#
# Usage:  .\deploy\build-launcher.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found at $python. Create it first (python -m venv .venv) and install requirements.txt."
}

& $python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pyinstaller (build-time only, not a runtime dependency)..."
    & $python -m pip install pyinstaller
}

& $python -m PyInstaller --onefile --noconsole --name "StartShiftRisk" `
    --distpath $root --workpath (Join-Path $root "build") --specpath (Join-Path $root "build") `
    (Join-Path $root "launcher.py")

Remove-Item (Join-Path $root "build") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`nDone: $root\StartShiftRisk.exe"
Write-Host "Double-click it to start the dashboard (or bring it to front if already running)."
