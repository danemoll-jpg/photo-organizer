@echo off
rem Double-click launcher for a clean restart of the Photo Organizer review
rem tool (CLAUDE.md rule 11 / TODO.md's "One-click restart" item).
rem
rem This exists because stale duplicate review_tool.py processes have
rem caused real confusion in this project more than once -- including a
rem real (if temporary) auth bypass, when a phone's request was served by
rem an old process that predated the Phase 2d login code entirely (see
rem CLAUDE.md's Project state). Python doesn't hot-reload; an old process
rem left running from an earlier session keeps serving old code
rem indefinitely.
rem
rem Unlike Launch Review Tool.bat, this does NOT need to worry about its
rem own console I/O crashing anything under pythonw.exe -- it just shells
rem out to main.py's own "restart-review-tool" command (a normal console
rem python.exe invocation), which does the actual work: find every
rem process bound to config.yaml's review_tool_port (there has been more
rem than one at once before -- kills ALL of them, not just the first),
rem wait for the port to actually free, then launch a fresh instance. The
rem dashboard's Remote Access panel's "Force Restart" button calls the
rem exact same underlying mechanism (src.port_check.restart_review_tool)
rem programmatically -- this .bat and that button are two front ends onto
rem one shared implementation, per this project's rule 7.
rem
rem The freshly-relaunched review_tool.py opens its own separate console
rem window (same as a normal Launch Review Tool.bat launch) and a browser
rem tab pointed at it -- this window can be closed once that's confirmed
rem up; closing THIS window (or letting it finish) does not stop the new
rem instance, which runs independently.
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

"%DIR%venv\Scripts\python.exe" "%DIR%main.py" restart-review-tool

echo.
pause
