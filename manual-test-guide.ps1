# Complete test: Stop services, run manually, test
Write-Host "=== Step 1: Stop services ===" -ForegroundColor Cyan

$nssmPath = "C:\Tools\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    $nssmPath = "nssm"
}

& $nssmPath stop clawrelay-api
& $nssmPath stop clawrelay-wecom
Start-Sleep -Seconds 2

Write-Host "Services stopped." -ForegroundColor Green
Write-Host ""
Write-Host "=== Step 2: Manual test ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Please run these commands in TWO separate PowerShell windows:" -ForegroundColor Yellow
Write-Host ""
Write-Host "Window 1 (API):" -ForegroundColor Cyan
Write-Host "  cd C:\next\clawrelay-api" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_AUTH_TOKEN='YOUR_ANTHROPIC_AUTH_TOKEN'" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_BASE_URL='https://your-api-endpoint.com'" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_MODEL='claude-sonnet-4-6'" -ForegroundColor White
Write-Host "  .\clawrelay-api.exe" -ForegroundColor White
Write-Host ""
Write-Host "Window 2 (WeCom):" -ForegroundColor Cyan
Write-Host "  cd C:\next\clawrelay-wecom-server" -ForegroundColor White
Write-Host "  python main.py" -ForegroundColor White
Write-Host ""
Write-Host "Then test Claude bot in WeChat." -ForegroundColor Yellow
Write-Host ""
Write-Host "If it works manually but not as service, the problem is:" -ForegroundColor Red
Write-Host "  1. Service user permissions" -ForegroundColor White
Write-Host "  2. Service environment inheritance" -ForegroundColor White
Write-Host "  3. Interactive vs non-interactive session" -ForegroundColor White
Write-Host ""
Read-Host "Press Enter when done testing"

Write-Host ""
Write-Host "=== Step 3: Restart services ===" -ForegroundColor Cyan
& $nssmPath start clawrelay-api
Start-Sleep -Seconds 2
& $nssmPath start clawrelay-wecom

Write-Host ""
Write-Host "Services restarted." -ForegroundColor Green
