@echo off
setlocal
title 3MF Plate Merger

:: Change working directory to the folder containing this batch script
cd /d "%~dp0"

echo ========================================================
echo           3MF Plate Merge Tool for Bambu Lab
echo ========================================================
echo.

:: Detect Python executable (checks 'python' then 'py' launcher)
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=python"
    goto :RUN
)

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=py"
    goto :RUN
)

echo [ERROR] Python was not found on your system!
echo Please ensure Python 3 is installed and added to PATH.
echo.
pause
exit /b 1

:RUN
echo Working directory: %~dp0
echo.

:: Execute the merge script with mech mode fast check disabled by default
"%PY_CMD%" merge_3mf.py --no-mech-mode %*

echo.
echo ========================================================
if %ERRORLEVEL% equ 0 (
    echo [SUCCESS] Merged 3MF created successfully!
) else (
    echo [ERROR] Merging failed with exit code %ERRORLEVEL%.
)
echo ========================================================
echo.
pause

