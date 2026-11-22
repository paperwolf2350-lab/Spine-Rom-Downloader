@echo off
rem Builds Spine-ROM-Downloader.exe from spine_rom_downloader.pyw.
rem Needs Python 3.11 or newer from python.org (with the "py launcher" option ticked).
cd /d "%~dp0"

py -3 --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
)

echo Installing the build tool (PyInstaller)...
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto failed

echo Building...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name Spine-ROM-Downloader spine_rom_downloader.pyw
if errorlevel 1 goto failed

echo.
echo Done. Your program is here:
echo   %~dp0dist\Spine-ROM-Downloader.exe
pause
exit /b 0

:failed
echo.
echo The build did not finish. The lines above say why.
pause
exit /b 1
