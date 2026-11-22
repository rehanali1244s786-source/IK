@echo off
setlocal
cd /d "%~dp0"
echo Installing build tools...
python -m pip install --upgrade -r requirements-build.txt || goto :fail
if not exist deno.exe (
  echo Downloading Deno ^(needed for YouTube^) - pinned version, SHA-256 checked...
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\get_deno.ps1 -Out . || goto :fail
)
set DENO=
if exist deno.exe set DENO=--add-binary "deno.exe;."
echo Building IK-DOWNLOADER.exe ...
python -m PyInstaller --noconfirm --onefile --noconsole --name "IK-DOWNLOADER" --icon assets\icon.ico --add-data "assets;assets" --collect-all yt_dlp --collect-all yt_dlp_ejs --collect-all imageio_ffmpeg --collect-all certifi %DENO% ik_downloader.py || goto :fail
echo Checking the built program...
start "" /wait dist\IK-DOWNLOADER.exe --selftest dist\selftest.json
powershell -NoProfile -Command "$r = Get-Content dist\selftest.json -Raw | ConvertFrom-Json; if (-not ($r.assets -and $r.tk -and $r.ffmpeg -and $r.yt_dlp)) { Write-Host ('SELF-TEST FAILED: ' + ($r | ConvertTo-Json -Compress)); exit 1 }" || goto :fail
del dist\selftest.json
echo.
echo Done. Your program: dist\IK-DOWNLOADER.exe
if /i not "%~1"=="nopause" pause
exit /b 0
:fail
echo.
echo BUILD FAILED.
if /i not "%~1"=="nopause" pause
exit /b 1
