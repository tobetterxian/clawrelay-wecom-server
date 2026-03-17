# Stop all running instances and test manually
Write-Host "Stopping all clawrelay processes..." -ForegroundColor Cyan

# Kill all processes
Get-Process -Name "clawrelay-api" -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {$_.Path -like "*clawrelay*"} | Stop-Process -Force

Start-Sleep -Seconds 2

Write-Host ""
Write-Host "All processes stopped." -ForegroundColor Green
Write-Host ""
Write-Host "Now please run these commands in TWO separate terminals:" -ForegroundColor Yellow
Write-Host ""
Write-Host "Terminal 1 (PowerShell):" -ForegroundColor Cyan
Write-Host "  cd C:\next\clawrelay-api" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_AUTH_TOKEN='YOUR_ANTHROPIC_AUTH_TOKEN'" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_BASE_URL='https://your-api-endpoint.com'" -ForegroundColor White
Write-Host "  `$env:ANTHROPIC_MODEL='claude-sonnet-4-6'" -ForegroundColor White
Write-Host "  .\clawrelay-api.exe" -ForegroundColor White
Write-Host ""
Write-Host "Terminal 2 (PowerShell):" -ForegroundColor Cyan
Write-Host "  cd C:\next\clawrelay-wecom-server" -ForegroundColor White
Write-Host "  python main.py" -ForegroundColor White
Write-Host ""
Write-Host "Then test Claude bot in WeChat." -ForegroundColor Yellow
Write-Host ""
Write-Host "If it works manually, the problem is definitely the service environment." -ForegroundColor Red
Write-Host ""
Read-Host "Press Enter to exit"
