@echo off
setlocal
cd /d "%~dp0"
rem 1) builds the program, 2) builds the Windows installer (needs Inno Setup 6: https://jrsoftware.org/isdl.php)
call build_exe.bat nopause || exit /b 1
for /f %%v in ('python tools\sync_version.py --print') do set VER=%%v
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php and run this file again.
  pause & exit /b 1
)
%ISCC% /DAppVersion=%VER% installer\ik_downloader.iss || (pause & exit /b 1)
echo.
echo Done. Installer: dist\IK-DOWNLOADER-Setup.exe
pause
