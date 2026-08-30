# ==============================================================================
# MyWhoosh App Monitor & Sync Trigger (PowerShell)
# Launches MyWhoosh, waits for it to close, then runs myWhoosh2Garmin.py
# ==============================================================================

$scriptDir = $PSScriptRoot
$pythonScript = Join-Path $scriptDir "myWhoosh2Garmin.py"

Write-Host "Waiting for MyWhoosh to start or already running..." -ForegroundColor Cyan

# Check if MyWhoosh is running (Windows or Mac process names)
function Get-MyWhooshProcess {
    return Get-Process -Name "*mywhoosh*", "*whoosh*" -ErrorAction SilentlyContinue | Select-Object -First 1
}

$proc = Get-MyWhooshProcess
if (-not $proc) {
    Write-Host "MyWhoosh is not currently running. Waiting for process to start..." -ForegroundColor Yellow
    while (-not ($proc = Get-MyWhooshProcess)) {
        Start-Sleep -Seconds 3
    }
}

Write-Host "Detected active MyWhoosh process (PID: $($proc.Id)). Monitoring..." -ForegroundColor Green

# Wait for application to exit
while ($null -ne (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
    Start-Sleep -Seconds 5
}

Write-Host "MyWhoosh has closed. Triggering synchronization script..." -ForegroundColor Cyan

# Execute Python script
if (Get-Command "python" -ErrorAction SilentlyContinue) {
    python "$pythonScript"
} elseif (Get-Command "py" -ErrorAction SilentlyContinue) {
    py -3 "$pythonScript"
} else {
    Write-Error "Python executable not found in PATH."
}
