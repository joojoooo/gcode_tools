@echo off
setlocal

REM OrcaSlicer / Bambu Studio Travel Optimization Launcher
REM Accepts G-code file path passed by slicer or dropped on script

set "PY_CMD="
where python >nul 2>&1
if %errorlevel% equ 0 set "PY_CMD=python"
if "%PY_CMD%"=="" (
    where py >nul 2>&1
    if %errorlevel% equ 0 set "PY_CMD=py"
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python was not found in your PATH.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Usage: optimize_travel.bat ^<path_to_gcode^>
    pause
    exit /b 1
)

"%PY_CMD%" "%~dp0optimize_travel.py" %*

