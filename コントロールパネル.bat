@echo off
rem ===========================================================================
rem  MUEStrategy control panel launcher (double-click me)
rem  - Uses pythonw.exe so no console window appears.
rem  - Requires kabu Station running and KABU_API_PASSWORD set (see README).
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat in the OEM code page
rem        (cp932 on Japanese Windows); UTF-8 Japanese breaks line parsing.
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PYW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
    set "PYW=%%P"
    goto :found
)
:found

if defined PYW (
    start "" "%PYW%" "ui\control_panel.pyw"
    exit /b 0
)

where python.exe >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install Python and add it to PATH.
    pause
    exit /b 1
)

echo pythonw.exe not found - falling back to python.exe (this window stays open).
python "ui\control_panel.pyw"
if errorlevel 1 pause
