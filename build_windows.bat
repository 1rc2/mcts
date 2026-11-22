@echo off
setlocal enabledelayedexpansion

rem ============================================================================
rem KuroMapHelper - Windows package build (PyInstaller onedir)
rem Equivalent manual command, run from the project root:
rem     pyinstaller packaging\kumap.spec --noconfirm
rem Output:  dist\<APP_NAME>\<APP_NAME>.exe   (APP_NAME default: KuroMapHelper)
rem To ship the exe under the Chinese product name, set KUMAP_EXE_NAME before
rem running this script; packaging\kumap.spec reads it.
rem See the "build exe" section of README.md.
rem
rem IMPORTANT: keep this file pure ASCII with CRLF line endings. cmd.exe
rem parses batch files with the OEM code page; non-ASCII text can break
rem commands inside parenthesized blocks even after "chcp 65001".
rem ============================================================================

cd /d "%~dp0"

if not exist "packaging\kumap.spec" (
    echo [ERROR] packaging\kumap.spec not found. Run this script from the project root.
    pause
    exit /b 1
)
if not exist "src\kumap\main.py" (
    echo [ERROR] src\kumap\main.py not found. Source tree is incomplete.
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
    echo         or set KUMAP_PYTHON to the full path of python.exe.
    pause
    exit /b 1
)

"%PYEXE%" %PYARGS% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    pause
    exit /b 1
)

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
if exist "%VPY%" (
    echo [1/3] Reusing existing venv .venv
) else (
    echo [1/3] Creating venv .venv ...
    "%PYEXE%" %PYARGS% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo [2/3] Installing build and runtime dependencies from requirements-dev.txt ...
"%VPY%" -m pip install --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. PyInstaller is required.
    echo         Check network / proxy and retry.
    echo         Slow network? Try a mirror, e.g.:
    echo             .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    pause
    exit /b 1
)

echo        Running PyInstaller, output directory: dist ...
"%VPY%" -m PyInstaller packaging\kumap.spec --noconfirm
if errorlevel 1 (
    echo [ERROR] Packaging failed. See the PyInstaller output above.
    echo         Common causes: antivirus block, dist directory in use,
    echo         missing Visual C++ runtime.
    pause
    exit /b 1
)

set "APPNAME=KuroMapHelper"
if defined KUMAP_EXE_NAME set "APPNAME=%KUMAP_EXE_NAME%"
set "OUTDIR=%~dp0dist\!APPNAME!"
set "OUTEXE=!OUTDIR!\!APPNAME!.exe"

if not exist "!OUTEXE!" (
    echo [ERROR] Build finished but the expected artifact is missing:
    echo         !OUTEXE!
    echo         Check dist\ and APP_NAME in packaging\kumap.spec.
    pause
    exit /b 1
)

echo [3/3] Build complete
echo ------------------------------------------------------------------
echo Executable:  !OUTEXE!
echo Ship the whole dist\!APPNAME! folder, not just the exe.
echo Local data directory, created on first run:
echo          %APPDATA%\KuroMapHelper
echo Portable mode: create dist\!APPNAME!\data and put an empty file
echo          named .portable inside it.
echo Vision positioning is disabled if OpenCV deps were not installed.
echo ------------------------------------------------------------------
exit /b 0
