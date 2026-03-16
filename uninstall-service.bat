@echo off
chcp 65001 >nul
REM ============================================
REM ClawRelay Service Uninstallation Script
REM ============================================

echo.
echo ============================================
echo ClawRelay Service Uninstallation
echo ============================================
echo.

REM Check if running as Administrator
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Please run as Administrator!
    pause
    exit /b 1
)

echo The following services will be uninstalled:
echo   1. clawrelay-api
echo   2. clawrelay-wecom
echo.

set /p CONFIRM="Confirm uninstallation? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo Uninstallation cancelled
    pause
    exit /b 0
)

echo.
echo [UNINSTALL] Stopping and removing services...
echo.

REM Stop and remove clawrelay-api
nssm stop clawrelay-api
nssm remove clawrelay-api confirm
echo [DONE] clawrelay-api uninstalled

REM Stop and remove clawrelay-wecom
nssm stop clawrelay-wecom
nssm remove clawrelay-wecom confirm
echo [DONE] clawrelay-wecom uninstalled

echo.
echo ============================================
echo Uninstallation Complete!
echo ============================================
echo.
pause
