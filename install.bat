@echo off
setlocal
REM One-time setup for SSGrab. Keep this file in the same folder as ssgrab.py.
REM Result: SSGrab.exe in this folder, build folders removed, Startup shortcut created.
cd /d "%~dp0"
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"

echo Installing Python packages...
python -m pip install --upgrade yt-dlp pystray pillow pyinstaller
echo.
echo Installing ffmpeg and Deno (needed for HD merges and YouTube)...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
echo.

call :build || goto :failed

echo Adding to Startup (starts minimized to tray)...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\SSGrab.lnk\"); $s.TargetPath='%HERE%\SSGrab.exe'; $s.Arguments='--minimized'; $s.WorkingDirectory='%HERE%'; $s.Save()"
echo.
echo Done. SSGrab.exe is in %HERE%
echo (Open a NEW terminal or sign out/in first so ffmpeg and Deno are on PATH.)
pause
exit /b 0

:build
echo Closing SSGrab if it is running...
taskkill /im SSGrab.exe /f >nul 2>&1
timeout /t 1 /nobreak >nul
echo Building SSGrab.exe ...
python -m PyInstaller --noconfirm --onefile --windowed --name SSGrab --collect-all yt_dlp ssgrab.py
if not exist "dist\SSGrab.exe" (
    echo Build failed - dist\SSGrab.exe was not created. Build folders left in place for troubleshooting.
    exit /b 1
)
copy /y "dist\SSGrab.exe" "%HERE%\SSGrab.exe" >nul || (
    echo Could not copy SSGrab.exe into %HERE% - is it still running?
    exit /b 1
)
echo Cleaning up build files...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
rmdir /s /q __pycache__ 2>nul
del /q SSGrab.spec 2>nul
exit /b 0

:failed
echo.
echo Setup did not finish.
pause
exit /b 1
