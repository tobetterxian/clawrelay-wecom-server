@echo off
chcp 65001 >nul
REM ============================================
REM ClawRelay Service Installation Script (NSSM)
REM ============================================

echo.
echo ============================================
echo ClawRelay Service Installation
echo ============================================
echo.

REM Check if NSSM is installed
where nssm >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] NSSM not found!
    echo.
    echo Please install NSSM first:
    echo   Method 1: Download from https://nssm.cc/download
    echo   Method 2: choco install nssm
    echo.
    pause
    exit /b 1
)

echo [CHECK] NSSM is installed
echo.

REM Check if Python is installed
where python >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Python not found!
    echo.
    echo Please install Python 3.12+
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('where python') do set PYTHON_PATH=%%i
echo [CHECK] Python path: %PYTHON_PATH%
echo.

REM Set default paths
set CLAWRELAY_API_PATH=C:\next\clawrelay-api
set WECOM_SERVER_PATH=C:\next\clawrelay-wecom-server

REM Ask user for paths
echo Please confirm paths (press Enter to use default):
echo.

set /p INPUT_API_PATH="clawrelay-api path [%CLAWRELAY_API_PATH%]: "
if not "%INPUT_API_PATH%"=="" set CLAWRELAY_API_PATH=%INPUT_API_PATH%

set /p INPUT_WECOM_PATH="clawrelay-wecom-server path [%WECOM_SERVER_PATH%]: "
if not "%INPUT_WECOM_PATH%"=="" set WECOM_SERVER_PATH=%INPUT_WECOM_PATH%

echo.
echo ============================================
echo Services to be installed:
echo ============================================
echo.
echo 1. clawrelay-api (EXE)
echo    Path: %CLAWRELAY_API_PATH%
echo.
echo 2. clawrelay-wecom (Python)
echo    Path: %WECOM_SERVER_PATH%
echo.
echo ============================================
echo.

set /p CONFIRM="Confirm installation? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo Installation cancelled
    pause
    exit /b 0
)

echo.
echo [INSTALL] Installing services...
echo.

REM Create log directories
if not exist "%CLAWRELAY_API_PATH%\logs" mkdir "%CLAWRELAY_API_PATH%\logs"
if not exist "%WECOM_SERVER_PATH%\logs" mkdir "%WECOM_SERVER_PATH%\logs"

REM Install clawrelay-api service (EXE)
echo [1/2] Installing clawrelay-api service (EXE)...
nssm install clawrelay-api "%CLAWRELAY_API_PATH%\clawrelay-api.exe" 2>nul
if %errorLevel% neq 0 (
    echo [WARNING] Service may already exist, trying to update...
    nssm stop clawrelay-api 2>nul
)
nssm set clawrelay-api AppDirectory "%CLAWRELAY_API_PATH%"
nssm set clawrelay-api Application "%CLAWRELAY_API_PATH%\clawrelay-api.exe"
nssm set clawrelay-api DisplayName "ClawRelay API Service"
nssm set clawrelay-api Description "ClawRelay API - Claude Code Relay Service"
nssm set clawrelay-api Start SERVICE_AUTO_START
nssm set clawrelay-api AppStdout "%CLAWRELAY_API_PATH%\logs\service.log"
nssm set clawrelay-api AppStderr "%CLAWRELAY_API_PATH%\logs\service-error.log"
nssm set clawrelay-api AppRotateFiles 1
nssm set clawrelay-api AppRotateBytes 10485760

echo [1/2] clawrelay-api service configured
echo.

REM Install clawrelay-wecom service (Python)
echo [2/2] Installing clawrelay-wecom service (Python)...
nssm install clawrelay-wecom "%PYTHON_PATH%" "%WECOM_SERVER_PATH%\main.py" 2>nul
if %errorLevel% neq 0 (
    echo [WARNING] Service may already exist, trying to update...
    nssm stop clawrelay-wecom 2>nul
)
nssm set clawrelay-wecom AppDirectory "%WECOM_SERVER_PATH%"
nssm set clawrelay-wecom Application "%PYTHON_PATH%"
nssm set clawrelay-wecom AppParameters "%WECOM_SERVER_PATH%\main.py"
nssm set clawrelay-wecom DisplayName "ClawRelay WeCom Server"
nssm set clawrelay-wecom Description "Enterprise WeChat Bot Service"
nssm set clawrelay-wecom Start SERVICE_AUTO_START
nssm set clawrelay-wecom AppStdout "%WECOM_SERVER_PATH%\logs\service.log"
nssm set clawrelay-wecom AppStderr "%WECOM_SERVER_PATH%\logs\service-error.log"
nssm set clawrelay-wecom AppRotateFiles 1
nssm set clawrelay-wecom AppRotateBytes 10485760

echo [2/2] clawrelay-wecom service configured
echo.

REM Start services
echo [START] Starting services...
echo.

nssm start clawrelay-api
if %errorLevel% equ 0 (
    echo [SUCCESS] clawrelay-api service started
) else (
    echo [INFO] clawrelay-api may already be running
)

timeout /t 2 /nobreak >nul

nssm start clawrelay-wecom
if %errorLevel% equ 0 (
    echo [SUCCESS] clawrelay-wecom service started
) else (
    echo [INFO] clawrelay-wecom may already be running
)

echo.
echo ============================================
echo Installation Complete!
echo ============================================
echo.
echo Service management commands:
echo   Check status: nssm status clawrelay-api
echo   Start:        nssm start clawrelay-api
echo   Stop:         nssm stop clawrelay-api
echo   Restart:      nssm restart clawrelay-api
echo   Uninstall:    nssm remove clawrelay-api confirm
echo.
echo View logs:
echo   API:   %CLAWRELAY_API_PATH%\logs\service.log
echo   WeCom: %WECOM_SERVER_PATH%\logs\service.log
echo.
echo Or use Windows Services Manager:
echo   services.msc
echo.
pause
