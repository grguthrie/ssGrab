@echo off
setlocal
REM When YouTube/Instagram downloads start failing: update yt-dlp and rebuild SSGrab.exe in place.
REM Keep this file in the same folder as ssgrab.py.
cd /d "%~dp0"
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"

python -m pip install --upgrade yt-dlp pyinstaller

echo Closing SSGrab if it is running...
taskkill /im SSGrab.exe /f >nul 2>&1
timeout /t 1 /nobreak >nul

python -m PyInstaller --noconfirm --onefile --windowed --name SSGrab --collect-all yt_dlp ssgrab.py
if not exist "dist\SSGrab.exe" (
    echo Build failed - dist\SSGrab.exe was not created. Your old SSGrab.exe is unchanged.
    pause
    exit /b 1
)
copy /y "dist\SSGrab.exe" "%HERE%\SSGrab.exe" >nul || (
    echo Could not replace %HERE%\SSGrab.exe - is it still running?
    pause
    exit /b 1
)
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
rmdir /s /q __pycache__ 2>nul
del /q SSGrab.spec 2>nul

echo Rebuilt %HERE%\SSGrab.exe - starting it...
start "" "%HERE%\SSGrab.exe" --minimized
pause
