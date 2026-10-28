@echo off
setlocal

title Bambu Studio G-Code Cleaner

REM Change working directory to the folder where this batch script is located
cd /d "%~dp0"

echo ============================================================
echo                 Bambu Studio G-Code Cleaner
echo ============================================================
echo Folder: %~dp0
echo.

REM Detect Python executable (checks python then py)
set "PY_CMD="
where python >nul 2>&1
if %errorlevel% equ 0 set "PY_CMD=python"
if "%PY_CMD%"=="" (
    where py >nul 2>&1
    if %errorlevel% equ 0 set "PY_CMD=py"
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python was not found in your system PATH.
    echo Please install Python from https://www.python.org or add it to your PATH.
    echo.
    pause
    exit /b 1
)

REM If files were dragged and dropped onto the bat, process them
REM Otherwise, automatically scan and clean all .gcode and .gcode.3mf in this folder
if "%~1"=="" (
    echo Scanning folder for .gcode and .gcode.3mf files...
    echo.
    "%PY_CMD%" "%~dp0clean_gcode.py" --folder "%~dp0."
) else (
    echo Processing provided files...
    echo.
    "%PY_CMD%" "%~dp0clean_gcode.py" %*
)

echo.
echo ============================================================
echo Cleaning process finished.
echo ============================================================
echo.
pause

