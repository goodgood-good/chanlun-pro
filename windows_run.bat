@echo off
setlocal enabledelayedexpansion

REM Resolve script directory (project root)
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"
echo ROOT_DIR: %ROOT_DIR%

REM Set PYTHONPATH
set "PYTHONPATH=%ROOT_DIR%src"
echo PYTHONPATH: !PYTHONPATH!

REM Start WEB service via poetry
echo Starting WEB service...
if exist "%ROOT_DIR%web\chanlun_chart\app.py" (
    poetry run python "%ROOT_DIR%web\chanlun_chart\app.py"
)
