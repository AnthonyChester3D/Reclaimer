@echo off
cd /d "%~dp0"
pip install pyinstaller pystray pillow
python -c "from reclaimer import write_ico; write_ico('reclaimer.ico')"
pyinstaller --onefile --noconsole --name reclaimer --icon reclaimer.ico --add-data "index.html;." reclaimer.py
echo.
echo Built dist\reclaimer.exe - put it in a folder of its own (settings and logs are written next to it).
echo It still needs ffmpeg: either installed on PATH, or copy ffmpeg.exe and ffprobe.exe next to it.
pause
