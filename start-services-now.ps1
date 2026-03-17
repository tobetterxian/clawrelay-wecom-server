# Start both services
Write-Host "Starting clawrelay-api..." -ForegroundColor Cyan

$apiProcess = Start-Process -FilePath "C:\next\clawrelay-api\clawrelay-api.exe" `
    -WorkingDirectory "C:\next\clawrelay-api" `
    -WindowStyle Minimized `
    -PassThru `
    -Environment @{
        ANTHROPIC_AUTH_TOKEN="YOUR_ANTHROPIC_AUTH_TOKEN"
        ANTHROPIC_BASE_URL="https://your-api-endpoint.com"
        ANTHROPIC_MODEL="claude-sonnet-4-6"
    }

Write-Host "API started (PID: $($apiProcess.Id))" -ForegroundColor Green

Start-Sleep -Seconds 3

Write-Host ""
Write-Host "Starting clawrelay-wecom..." -ForegroundColor Cyan

$wecomProcess = Start-Process -FilePath "python" `
    -ArgumentList "main.py" `
    -WorkingDirectory "C:\next\clawrelay-wecom-server" `
    -WindowStyle Minimized `
    -PassThru

Write-Host "WeCom started (PID: $($wecomProcess.Id))" -ForegroundColor Green

Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Checking processes..." -ForegroundColor Yellow
Get-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime
Get-Process -Id $wecomProcess.Id -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime

Write-Host ""
Write-Host "Services started. Please test Claude bot now." -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to exit"
