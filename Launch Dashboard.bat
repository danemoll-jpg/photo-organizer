@echo off
rem Double-click launcher for the Photo Organizer dashboard.
rem Uses pythonw.exe (no console window) so this just opens the GUI.
setlocal
set "DIR=%~dp0"

if not exist "%DIR%venv\Scripts\pythonw.exe" (
    echo venv not found at "%DIR%venv".
    echo Run setup first:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

start "" "%DIR%venv\Scripts\pythonw.exe" "%DIR%dashboard.py"
