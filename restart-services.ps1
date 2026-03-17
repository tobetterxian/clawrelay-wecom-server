# Restart services and test immediately
$ErrorActionPreference = "Stop"

$nssmPath = "C:\Tools\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    $nssmPath = "nssm"
}

Write-Host "Restarting services..." -ForegroundColor Cyan

& $nssmPath restart clawrelay-api
Start-Sleep -Seconds 3

& $nssmPath restart clawrelay-wecom
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Services restarted. Status:" -ForegroundColor Green
& $nssmPath status clawrelay-api
& $nssmPath status clawrelay-wecom

Write-Host ""
Write-Host "Please test Claude bot now by sending a message." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to exit"
