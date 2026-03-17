# Create startup scripts that run in user context
Write-Host "Creating startup scripts..." -ForegroundColor Cyan
Write-Host ""

# Create API startup script
$apiScript = @'
@echo off
cd /d C:\next\clawrelay-api
set ANTHROPIC_AUTH_TOKEN=YOUR_ANTHROPIC_AUTH_TOKEN
set ANTHROPIC_BASE_URL=https://your-api-endpoint.com
set ANTHROPIC_MODEL=claude-sonnet-4-6
start "ClawRelay API" /MIN clawrelay-api.exe
'@

$apiScript | Out-File -FilePath "C:\next\clawrelay-api\start-api.bat" -Encoding ASCII

# Create WeCom startup script
$wecomScript = @'
@echo off
cd /d C:\next\clawrelay-wecom-server
start "ClawRelay WeCom" /MIN python main.py
'@

$wecomScript | Out-File -FilePath "C:\next\clawrelay-wecom-server\start-wecom.bat" -Encoding ASCII

Write-Host "Created startup scripts:" -ForegroundColor Green
Write-Host "  C:\next\clawrelay-api\start-api.bat" -ForegroundColor White
Write-Host "  C:\next\clawrelay-wecom-server\start-wecom.bat" -ForegroundColor White

Write-Host ""
Write-Host "Now adding to Windows startup..." -ForegroundColor Yellow

# Create shortcuts in Startup folder
$shell = New-Object -ComObject WScript.Shell
$startupFolder = [Environment]::GetFolderPath('Startup')

# API shortcut
$apiShortcut = $shell.CreateShortcut("$startupFolder\ClawRelay API.lnk")
$apiShortcut.TargetPath = "C:\next\clawrelay-api\start-api.bat"
$apiShortcut.WorkingDirectory = "C:\next\clawrelay-api"
$apiShortcut.WindowStyle = 7  # Minimized
$apiShortcut.Save()

# WeCom shortcut
$wecomShortcut = $shell.CreateShortcut("$startupFolder\ClawRelay WeCom.lnk")
$wecomShortcut.TargetPath = "C:\next\clawrelay-wecom-server\start-wecom.bat"
$wecomShortcut.WorkingDirectory = "C:\next\clawrelay-wecom-server"
$wecomShortcut.WindowStyle = 7  # Minimized
$wecomShortcut.Save()

Write-Host ""
Write-Host "Added to startup folder: $startupFolder" -ForegroundColor Green

Write-Host ""
Write-Host "Now stopping and removing Windows services..." -ForegroundColor Yellow

$nssmPath = "C:\Tools\nssm\nssm.exe"
if (-not (Test-Path $nssmPath)) {
    $nssmPath = "nssm"
}

& $nssmPath stop clawrelay-api
& $nssmPath stop clawrelay-wecom
Start-Sleep -Seconds 2

& $nssmPath remove clawrelay-api confirm
& $nssmPath remove clawrelay-wecom confirm

Write-Host ""
Write-Host "Services removed." -ForegroundColor Green

Write-Host ""
Write-Host "Testing startup scripts..." -ForegroundColor Yellow
Write-Host "Starting API..." -ForegroundColor Cyan
Start-Process -FilePath "C:\next\clawrelay-api\start-api.bat" -WindowStyle Minimized
Start-Sleep -Seconds 3

Write-Host "Starting WeCom..." -ForegroundColor Cyan
Start-Process -FilePath "C:\next\clawrelay-wecom-server\start-wecom.bat" -WindowStyle Minimized
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Done! Programs are now running." -ForegroundColor Green
Write-Host "They will auto-start when you log in." -ForegroundColor Green
Write-Host ""
Write-Host "Please test Claude bot now." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to exit"
