@echo off
setlocal enabledelayedexpansion

REM Resolve script directory (project root)
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"
if errorlevel 1 goto :fail
echo ROOT_DIR: %ROOT_DIR%

REM 1. Install poetry (matches CI; requires Python 3.10+ already on PATH)
echo 1. Installing poetry...
pip install poetry
if errorlevel 1 goto :fail

REM 2. Install dependencies (core only by default).
REM    Optional extras: us / hk / cn-extra / futures / ai / notify / backtest / monitor / charts
REM    e.g.  poetry install --extras us --extras hk
REM          poetry install --all-extras
echo 2. Installing dependencies...
poetry install
if errorlevel 1 goto :fail

REM 3. Create config.py from template if missing
echo 3. Preparing config file...
if not exist "%ROOT_DIR%src\chanlun\config.py" (
    copy "%ROOT_DIR%src\chanlun\config.py.demo" "%ROOT_DIR%src\chanlun\config.py" >nul
    if errorlevel 1 goto :fail
)

REM 4. Set PYTHONPATH
set "PYTHONPATH=%ROOT_DIR%src"
echo PYTHONPATH: !PYTHONPATH!

REM 5. Run environment check
echo 5. Running env check...
if not exist "%ROOT_DIR%check_env.py" (
    echo ERROR: check_env.py not found.
    goto :fail
)
poetry run python "%ROOT_DIR%check_env.py"
if errorlevel 1 goto :fail

echo Install complete.
pause
exit /b 0

:fail
echo Installation failed. See errors above.
exit /b 1
