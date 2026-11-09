@echo off
cd /d "%~dp0"
pip install pyinstaller pystray pillow
pyinstaller --onefile --noconsole --name reclaimer --add-data "index.html;." reclaimer.py
echo.
echo Built dist\reclaimer.exe - put it in a folder of its own (settings and logs are written next to it).
echo It still needs ffmpeg: either installed on PATH, or copy ffmpeg.exe and ffprobe.exe next to it.
pause
