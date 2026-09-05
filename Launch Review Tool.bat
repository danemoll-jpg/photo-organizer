@echo off
rem Double-click launcher for the Photo Organizer review tool (Phase 2b).
rem
rem Unlike Launch Dashboard.bat, this uses python.exe (WITH a console
rem window), not pythonw.exe -- deliberately. review_tool.py is a local
rem web server: it prints its own startup message (the URL to open) and
rem Flask/Werkzeug logs each request to the console, both of which need a
rem real stdout/stderr to write to. Under pythonw.exe those are None, and
rem the very first print() would crash immediately -- the same crash
rem class CLAUDE.md already documents for tqdm/hachoir, just self-inflicted
rem this time instead of a third-party library's fault.
rem
rem Leave this window open while you're using the tool in the browser;
rem close the window (or Ctrl+C inside it) to stop the server.
setlocal
set "DIR=%~dp0"

if not exist "%DIR%venv\Scripts\python.exe" (
    echo venv not found at "%DIR%venv".
    echo Run setup first:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

"%DIR%venv\Scripts\python.exe" "%DIR%review_tool.py"

echo.
echo Review tool stopped.
pause
