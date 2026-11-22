@echo off
setlocal

rem ============================================================================
rem KuroMapHelper - exploration guide launcher
rem   Opens the official browser map only, enables screen-vision position sync,
rem   plans a continuous nearest-neighbour route and guides stop by stop.
rem
rem Requirements: Python 3.10+ and .venv created by run_dev.bat; vision deps
rem (opencv-python / numpy / mss) for automatic position sync.
rem Keep this file pure ASCII with CRLF line endings.
rem ============================================================================

cd /d "%~dp0"

if not exist "src\kumap\main.py" (
    echo [ERROR] src\kumap\main.py not found. Run this script from the project root.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run run_dev.bat once to create it.
    pause
    exit /b 1
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -c "import cv2, numpy, mss" >nul 2>nul
if errorlevel 1 (
    echo [WARN] Vision dependencies missing. The guide can still use manual positioning.
    echo        Install with: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
)

echo Starting exploration guide...
echo   Official map + live position + automatic route planning.
".venv\Scripts\python.exe" -m kumap.guide %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo [ERROR] Guide exited with code %RC%
    echo         Log file: %APPDATA%\KuroMapHelper\app.log
    pause
)
exit /b %RC%
