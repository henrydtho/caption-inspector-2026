@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."

py -3 "%REPO_ROOT%\packaging\build_offline_app_windows.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Caption Inspector offline build failed. Press any key to close.
    pause >nul
)

exit /b %EXIT_CODE%
