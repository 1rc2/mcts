@echo off
setlocal

rem ============================================================================
rem KuroMapHelper - dev launcher (Windows)
rem   1. detect python (fallback: py -3), require >= 3.10
rem   2. create / reuse a venv at .venv
rem   3. pip install -r requirements.txt
rem   4. set PYTHONPATH=src and run: python -m kumap.main
rem
rem Extra args are forwarded, e.g.  run_dev.bat --no-tray --compact
rem Optional: set KUMAP_PYTHON to a full python.exe path to override.
rem
rem IMPORTANT: keep this file pure ASCII with CRLF line endings. cmd.exe
rem parses batch files with the OEM code page; non-ASCII text can break
rem commands inside parenthesized blocks even after "chcp 65001".
rem ============================================================================

cd /d "%~dp0"

if not exist "src\kumap\main.py" (
    echo [ERROR] src\kumap\main.py not found. Run this script from the project root.
    pause
    exit /b 1
)

set "PYEXE="
set "PYARGS="
if defined KUMAP_PYTHON if exist "%KUMAP_PYTHON%" set "PYEXE=%KUMAP_PYTHON%"
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)
if not defined PYEXE (
    echo [ERROR] Python not found. Install Python 3.10+, 3.12 recommended.
    echo         and enable "Add python.exe to PATH", or set KUMAP_PYTHON
    echo         to the full path of python.exe.
    pause
    exit /b 1
)

echo [1/4] Interpreter: %PYEXE% %PYARGS%
"%PYEXE%" %PYARGS% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    pause
    exit /b 1
)

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
if exist "%VPY%" (
    echo [2/4] Reusing existing venv .venv
) else (
    echo [2/4] Creating venv .venv ...
    "%PYEXE%" %PYARGS% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo [3/4] Installing dependencies from requirements.txt ...
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo         Common causes: no network, proxy auth required, read-only disk.
    echo         Manual command:
    echo             .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo         Slow network? Try a mirror, e.g.:
    echo             .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    pause
    exit /b 1
)

set "PYTHONPATH=%~dp0src"
echo [4/4] Starting KuroMapHelper dev mode ...
echo        Vision positioning is disabled until OpenCV deps are installed.
echo        Optional: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
"%VPY%" -m kumap.main %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo [ERROR] App exited with code %RC%
    echo         Log file: %APPDATA%\KuroMapHelper\app.log
    pause
)
exit /b %RC%
