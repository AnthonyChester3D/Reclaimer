@echo off
rem Builds reclaimer.exe and packs it into release\Reclaimer-v<version>-windows.zip for a GitHub release.
cd /d "%~dp0"
for /f "delims=" %%v in ('python -c "from reclaimer import APP_VERSION; print(APP_VERSION)"') do set VER=%%v
if "%VER%"=="" (echo Couldn't read the version from reclaimer.py & pause & exit /b 1)
echo Building Reclaimer %VER% ...
pip install --quiet --upgrade pyinstaller pystray pillow || (echo pip failed & pause & exit /b 1)
python -c "from reclaimer import write_ico; write_ico('reclaimer.ico')"
pyinstaller --noconfirm --clean --onefile --noconsole --name reclaimer --icon reclaimer.ico --add-data "index.html;." reclaimer.py || (echo Build failed & pause & exit /b 1)
if not exist release mkdir release
set ZIP=release\Reclaimer-v%VER%-windows.zip
if exist "%ZIP%" del "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path dist\reclaimer.exe, README.md, LICENSE -DestinationPath '%ZIP%'"
powershell -NoProfile -Command "(Get-FileHash '%ZIP%' -Algorithm SHA256).Hash | Out-File -Encoding ascii '%ZIP%.sha256'"
echo.
echo Done: %ZIP%
echo Test it: unzip into an empty folder and run reclaimer.exe before publishing.
pause
