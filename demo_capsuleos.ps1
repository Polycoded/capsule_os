$ErrorActionPreference = "Stop"

$Port = if ($env:CAPSULEOS_PORT) { $env:CAPSULEOS_PORT } else { "8768" }
$BaseUrl = "http://127.0.0.1:$Port"
$BundledPython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = if (Test-Path $BundledPython) { $BundledPython } else { "python" }
$LocalDependencies = Join-Path $PSScriptRoot ".deps"
$LogDir = Join-Path $PSScriptRoot "data"
$OutLogPath = Join-Path $LogDir "fastapi_demo_server.out.log"
$ErrLogPath = Join-Path $LogDir "fastapi_demo_server.err.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path $LocalDependencies) {
    $env:PYTHONPATH = "$LocalDependencies;$env:PYTHONPATH"
}

try {
    Invoke-WebRequest -UseBasicParsing "$BaseUrl/api/capsules" | Out-Null
    Write-Host "Using existing CapsuleOS FastAPI server at $BaseUrl"
} catch {
    Write-Host "Starting CapsuleOS FastAPI server at $BaseUrl"
    $env:CAPSULEOS_PORT = $Port
    $Cmd = "set CAPSULEOS_PORT=$Port&& `"$Python`" server.py > `"$OutLogPath`" 2> `"$ErrLogPath`""
    Start-Process `
        -FilePath "cmd.exe" `
        -ArgumentList "/c", $Cmd `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden

    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-WebRequest -UseBasicParsing "$BaseUrl/api/capsules" | Out-Null
            $ready = $true
            break
        } catch {}
    }
    if (-not $ready) {
        throw "CapsuleOS FastAPI server did not become ready. See $OutLogPath and $ErrLogPath"
    }
}

& $Python "demos/run_working_demo.py" --base-url $BaseUrl

Write-Host ""
Write-Host "Dashboard: $BaseUrl"
Write-Host "API docs:  $BaseUrl/docs"
