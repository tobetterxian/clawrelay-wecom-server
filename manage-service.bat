@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
REM ============================================
REM ClawRelay Service Management Script
REM ============================================

:MENU
echo.
echo ============================================
echo ClawRelay Service Management
echo ============================================
echo.
echo 1. Check service status
echo 2. Start all services
echo 3. Stop all services
echo 4. Restart all services
echo 5. View logs (clawrelay-wecom)
echo 6. View logs (clawrelay-api)
echo 7. Fix paused services
echo 8. Configure API environment (third-party API)
echo 9. Exit
echo.
set /p CHOICE="Select option (1-9): "

if "%CHOICE%"=="1" goto STATUS
if "%CHOICE%"=="2" goto START
if "%CHOICE%"=="3" goto STOP
if "%CHOICE%"=="4" goto RESTART
if "%CHOICE%"=="5" goto LOG_WECOM
if "%CHOICE%"=="6" goto LOG_API
if "%CHOICE%"=="7" goto FIX_PAUSED
if "%CHOICE%"=="8" goto CONFIG_API_ENV
if "%CHOICE%"=="9" goto END
goto MENU

:STATUS
echo.
echo [STATUS] Checking service status...
echo.
nssm status clawrelay-api
echo.
nssm status clawrelay-wecom
echo.
pause
goto MENU

:START
echo.
echo [START] Starting services...
echo.
nssm start clawrelay-api
echo.
nssm start clawrelay-wecom
echo.
pause
goto MENU

:STOP
echo.
echo [STOP] Stopping services...
echo.
nssm stop clawrelay-api
echo.
nssm stop clawrelay-wecom
echo.
pause
goto MENU

:RESTART
echo.
echo [RESTART] Restarting services...
echo.
nssm restart clawrelay-api
timeout /t 2 /nobreak >nul
echo.
nssm restart clawrelay-wecom
echo.
pause
goto MENU

:LOG_WECOM
echo.
echo [LOG] clawrelay-wecom service log (last 50 lines):
echo.
powershell -Command "Get-Content 'C:\next\clawrelay-wecom-server\logs\service.log' -Tail 50 -Encoding UTF8"
echo.
pause
goto MENU

:LOG_API
echo.
echo [LOG] clawrelay-api service log (last 50 lines):
echo.
powershell -Command "Get-Content 'C:\clawrelay-api\logs\service.log' -Tail 50 -Encoding UTF8"
echo.
pause
goto MENU

:FIX_PAUSED
echo.
echo [FIX] Fixing paused services...
echo.
echo Stopping and reinstalling clawrelay-api...
nssm stop clawrelay-api
timeout /t 2 /nobreak >nul
taskkill /F /IM clawrelay-api.exe 2>nul
timeout /t 1 /nobreak >nul

echo Removing service...
nssm remove clawrelay-api confirm
timeout /t 1 /nobreak >nul

echo Reinstalling service...
set CLAWRELAY_API_PATH=C:\next\clawrelay-api
nssm install clawrelay-api "%CLAWRELAY_API_PATH%\clawrelay-api.exe"
nssm set clawrelay-api AppDirectory "%CLAWRELAY_API_PATH%"
nssm set clawrelay-api DisplayName "ClawRelay API Service"
nssm set clawrelay-api Start SERVICE_AUTO_START
nssm set clawrelay-api AppStdout "%CLAWRELAY_API_PATH%\logs\service.log"
nssm set clawrelay-api AppStderr "%CLAWRELAY_API_PATH%\logs\service-error.log"

echo Starting service...
nssm start clawrelay-api
timeout /t 2 /nobreak >nul

echo.
echo Checking clawrelay-wecom...
nssm status clawrelay-wecom
echo.
echo Done! Check status to verify.
pause
goto MENU

:CONFIG_API_ENV
echo.
echo ============================================
echo Configure clawrelay-api Environment
echo ============================================
echo.
echo This will configure clawrelay-api service using config\api-service.env
echo.
echo Reading configuration from config\api-service.env...
echo.

REM Check if config file exists
if not exist "config\api-service.env" (
    echo [ERROR] Configuration file not found: config\api-service.env
    echo.
    pause
    goto MENU
)

REM Read configuration file
set CLAUDE_BIN_PATH=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_BASE_URL=
set ANTHROPIC_DEFAULT_HAIKU_MODEL=
set ANTHROPIC_DEFAULT_OPUS_MODEL=
set ANTHROPIC_DEFAULT_SONNET_MODEL=
set ANTHROPIC_MODEL=

for /f "usebackq tokens=1,* delims==" %%a in ("config\api-service.env") do (
    set LINE=%%a
    REM Skip comments and empty lines
    if not "!LINE:~0,1!"=="#" if not "!LINE!"=="" (
        set %%a=%%b
    )
)

REM Display configuration
echo Configuration loaded:
echo   Claude Path: %CLAUDE_BIN_PATH%
if not "%ANTHROPIC_BASE_URL%"=="" (
    echo   Third-party API: %ANTHROPIC_BASE_URL%
    echo   API Key: %ANTHROPIC_AUTH_TOKEN:~0,20%...
) else (
    echo   Third-party API: Not configured (using official API)
)
echo.

set /p CONFIRM="Apply this configuration? (Y/N): "
if /i not "%CONFIRM%"=="Y" goto MENU

echo.
echo Applying configuration...

REM Set PATH with Claude CLI
if not "%CLAUDE_BIN_PATH%"=="" (
    echo Setting PATH with Claude CLI...
    nssm set clawrelay-api AppEnvironmentExtra PATH=%CLAUDE_BIN_PATH%;%%PATH%%
)

REM Set third-party API variables if configured
if not "%ANTHROPIC_AUTH_TOKEN%"=="" (
    echo Setting third-party API configuration...
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_AUTH_TOKEN=%ANTHROPIC_AUTH_TOKEN%
)

if not "%ANTHROPIC_BASE_URL%"=="" (
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_BASE_URL=%ANTHROPIC_BASE_URL%
)

if not "%ANTHROPIC_DEFAULT_HAIKU_MODEL%"=="" (
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_DEFAULT_HAIKU_MODEL=%ANTHROPIC_DEFAULT_HAIKU_MODEL%
)

if not "%ANTHROPIC_DEFAULT_OPUS_MODEL%"=="" (
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_DEFAULT_OPUS_MODEL=%ANTHROPIC_DEFAULT_OPUS_MODEL%
)

if not "%ANTHROPIC_DEFAULT_SONNET_MODEL%"=="" (
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_DEFAULT_SONNET_MODEL=%ANTHROPIC_DEFAULT_SONNET_MODEL%
)

if not "%ANTHROPIC_MODEL%"=="" (
    nssm set clawrelay-api AppEnvironmentExtra ANTHROPIC_MODEL=%ANTHROPIC_MODEL%
)

echo.
echo Restarting services...
nssm restart clawrelay-api
timeout /t 2 /nobreak >nul
nssm restart clawrelay-wecom

echo.
echo Done! Configuration applied.
echo.
echo To modify configuration, edit: config\api-service.env
echo Then run this option again to apply changes.
echo.
pause
goto MENU

:END
echo.
echo Goodbye!
exit /b 0
