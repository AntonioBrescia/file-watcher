@echo off
REM Crea FileWatcher.exe (singolo file, senza finestra console) in dist\
REM Prerequisito: pip install -r requirements.txt

pyinstaller --onefile --noconsole --name FileWatcher file_watcher.py

echo.
echo Fatto! L'eseguibile si trova in: dist\FileWatcher.exe
pause
