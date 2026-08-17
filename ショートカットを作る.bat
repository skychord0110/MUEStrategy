@echo off
rem ===========================================================================
rem  Create a desktop shortcut for the control panel, then pin it to taskbar.
rem  Japanese guidance is printed by ui\make_shortcut.py (cmd cannot handle
rem  UTF-8 Japanese in .bat reliably).
rem ===========================================================================
setlocal
cd /d "%~dp0"

where python.exe >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install Python and add it to PATH.
    pause
    exit /b 1
)

python "ui\make_shortcut.py"
echo.
pause
