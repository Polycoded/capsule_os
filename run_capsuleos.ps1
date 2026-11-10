$BundledPython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$LocalDependencies = Join-Path $PSScriptRoot ".deps"

if (Test-Path $LocalDependencies) {
    $env:PYTHONPATH = "$LocalDependencies;$env:PYTHONPATH"
}

if (Test-Path $BundledPython) {
    & $BundledPython server.py
} else {
    python server.py
}
