@echo off
rem ============================================================================
rem MUEStrategy コントロールパネル
rem   このファイルをダブルクリックすると設定画面が開きます。
rem   （黒いコンソール窓は出ません。pythonw.exe で起動するため）
rem
rem   前提: kabuステーションを起動してログインしておくこと
rem         環境変数 KABU_API_PASSWORD を setx で設定しておくこと
rem ============================================================================
setlocal
cd /d "%~dp0"

rem pythonw.exe（窓なし版）を探す。見つからなければ python.exe で代用する。
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
    echo Python が見つかりません。
    echo   https://www.python.org/ からインストールし、PATH に追加してください。
    pause
    exit /b 1
)

echo pythonw.exe が見つからないため python.exe で起動します（この窓は開いたままになります）。
python "ui\control_panel.pyw"
if errorlevel 1 pause
