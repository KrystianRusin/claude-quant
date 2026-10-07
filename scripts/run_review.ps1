# Run the nightly review through Git Bash (for Task Scheduler).
$root = Split-Path -Parent $PSScriptRoot
$bash = "C:\Program Files\Git\bin\bash.exe"
& $bash -l ((Join-Path $root "review\run_review.sh") -replace '\\', '/')
exit $LASTEXITCODE
