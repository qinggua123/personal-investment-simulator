$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectDir
try {
    python -m uvicorn app:app --host 127.0.0.1 --port 8765
}
finally {
    Pop-Location
}
