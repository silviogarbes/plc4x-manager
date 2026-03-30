@echo off
REM =============================================================
REM PLC4X Manager — Update Script (Windows)
REM =============================================================
REM Usage:
REM   update.bat              — Update to latest version
REM   update.bat v1.2.0       — Update to specific version
REM   update.bat --check      — Check for updates without applying
REM =============================================================

echo.
echo =============================================================
echo   PLC4X Manager — Update Tool
echo =============================================================
echo.

REM Get current version
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set CURRENT=%%i
for /f "delims=" %%i in ('git describe --tags --exact-match 2^>nul') do set CURTAG=%%i
if "%CURTAG%"=="" set CURTAG=untagged
echo   Current version: %CURTAG% (%CURRENT%)

REM Fetch updates
echo   Fetching updates from GitHub...
git fetch origin --tags --quiet 2>nul
if errorlevel 1 (
    echo   ERROR: Cannot reach GitHub. Check your network.
    exit /b 1
)

for /f "delims=" %%i in ('git rev-parse --short origin/main 2^>nul') do set LATEST=%%i
echo   Latest commit:   %LATEST%
echo.

REM Check mode
if "%1"=="--check" (
    if "%CURRENT%"=="%LATEST%" (
        echo   You are up to date.
    ) else (
        echo   Update available!
        echo.
        echo   Recent changes:
        git log --oneline HEAD..origin/main 2>nul
    )
    echo.
    exit /b 0
)

REM Update
if "%CURRENT%"=="%LATEST%" (
    echo   Already up to date.
    echo.
    exit /b 0
)

echo   Changes available:
git log --oneline HEAD..origin/main 2>nul
echo.

set /p confirm="  Apply update and rebuild? [y/N] "
if /i not "%confirm%"=="y" (
    echo   Cancelled.
    exit /b 0
)

echo.
echo   [1/3] Pulling changes...
git pull origin main --ff-only
if errorlevel 1 (
    echo   ERROR: Cannot fast-forward. Local changes may conflict.
    exit /b 1
)

echo   [2/3] Rebuilding containers...
docker compose build

echo   [3/3] Restarting...
docker compose up -d

echo.
echo =============================================================
echo   Update complete!
echo =============================================================
echo.
