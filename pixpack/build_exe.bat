@echo off
setlocal
cd /d "%~dp0."
if errorlevel 1 (
    echo Cannot enter the script directory.
    exit /b 1
)
py -3 -V >nul 2>&1
if errorlevel 9009 (
    echo Cannot find the Python launcher py. Install Python 3.
    exit /b 9009
)
if errorlevel 1 (
    echo Python launcher py failed.
    exit /b 1
)
tasklist /FI "IMAGENAME eq PixPack.exe" | find /I "PixPack.exe" >nul
if not errorlevel 1 (
    echo Closing the running PixPack.exe so it can be replaced.
    taskkill /F /IM PixPack.exe >nul 2>&1
)
set /a TRIES=0
:unlock
if not exist dist\PixPack.exe goto :unlocked
del /f /q dist\PixPack.exe >nul 2>&1
if not exist dist\PixPack.exe goto :unlocked
set /a TRIES+=1
if %TRIES% GEQ 5 (
    echo dist\PixPack.exe is still in use. Close it and run this again.
    exit /b 1
)
ping -n 2 127.0.0.1 >nul
goto :unlock
:unlocked
py -3 -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 (
    echo Failed to install build dependencies.
    exit /b 1
)
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name PixPack --collect-submodules PIL pixpack_gui.py
if errorlevel 1 (
    echo Failed to build dist\PixPack.exe
    exit /b 1
)
echo.
echo Built dist\PixPack.exe
exit /b 0
