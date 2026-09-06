@echo off
rem Double-click launcher for the Phase 2d Cloudflare Tunnel that exposes
rem review_tool.py to the internet over real HTTPS, without opening any
rem inbound port on the router. Independent of Plex's own remote access.
rem
rem One-time setup (per machine) BEFORE this launcher will work -- see
rem README.md's "Remote access" section for the full walkthrough:
rem   1. cloudflared tunnel login
rem   2. cloudflared tunnel create photo-viewer
rem   3. Write %USERPROFILE%\.cloudflared\config.yml (see
rem      cloudflared-config.example.yml in this repo for the template)
rem   4. cloudflared tunnel route dns photo-viewer <your-hostname>
rem
rem "photo-viewer" is this project's actual tunnel name (already created
rem and confirmed working end-to-end -- see CLAUDE.md's Phase 2d
rem checkpoint), not a placeholder -- also config-driven as
rem cloudflare_tunnel_name in config.yaml for dashboard.py's Remote Access
rem panel, which runs the exact same `cloudflared tunnel run` command.
rem
rem review_tool.py itself must ALSO be running (Launch Review Tool.bat) --
rem this tunnel only forwards to http://127.0.0.1:<review_tool_port>, it
rem doesn't start the app itself. Leave both windows open while sharing
rem access; closing this one takes the tunnel down (review_tool.py keeps
rem running locally, just no longer reachable from outside).
setlocal

where cloudflared >nul 2>nul
if errorlevel 1 (
    echo cloudflared not found on PATH. Install it first:
    echo   winget install --id Cloudflare.cloudflared -e
    echo Then open a NEW terminal (PATH only refreshes for new windows^) and re-run this.
    pause
    exit /b 1
)

if not exist "%USERPROFILE%\.cloudflared\config.yml" (
    echo No tunnel config found at %USERPROFILE%\.cloudflared\config.yml
    echo Complete the one-time setup in README.md's "Remote access" section first
    echo ^(cloudflared tunnel login / create / route dns^) -- see
    echo cloudflared-config.example.yml in this repo for the config template.
    pause
    exit /b 1
)

cloudflared tunnel run photo-viewer

echo.
echo Tunnel stopped -- review_tool.py is no longer reachable remotely (it's
echo still running locally if you left its own window open).
pause
