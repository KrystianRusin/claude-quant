# Start today's live paper session, plus the shadow dry run when config.shadow.json exists.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path "config.shadow.json") {
    Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList "trader.py --dry-run --config config.shadow.json --data-dir data/shadow"
}
& $py trader.py
exit $LASTEXITCODE
