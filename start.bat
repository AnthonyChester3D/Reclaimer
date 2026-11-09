@echo off
cd /d "%~dp0"

where python >nul 2>nul || goto :nopython
python -c "import pystray, PIL" >nul 2>nul || (echo Installing the system tray helper - one time only... & python -m pip install --quiet --disable-pip-version-check pystray pillow)
where ffmpeg >nul 2>nul && goto :run
if exist ffmpeg.exe goto :run
where winget >nul 2>nul || goto :manual
echo ffmpeg not found - installing it with winget...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements || goto :manual

:run
rem pythonw runs Reclaimer without a console window; it lives in the system tray next to the clock
where pythonw >nul 2>nul && (start "" pythonw reclaimer.py & exit /b)
python reclaimer.py
exit /b

:manual
echo Could not install ffmpeg automatically. Opening the download page...
start "" https://www.gyan.dev/ffmpeg/builds/
echo Download ffmpeg-release-full.7z, then copy ffmpeg.exe and ffprobe.exe from its bin folder into:
echo   %~dp0
echo Reclaimer will open now and show the same steps, with a "Check again" button.
timeout /t 5 >nul
goto :run

:nopython
where winget >nul 2>nul || (echo Python is not installed. Opening python.org... & start "" https://www.python.org/downloads/ & echo Install it, tick "Add python.exe to PATH", then run start.bat again. & pause & exit /b)
echo Python not found - installing it with winget...
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
echo.
echo Done. Close this window and run start.bat again.
pause
exit /b
