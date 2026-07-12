@echo off
setlocal EnableExtensions DisableDelayedExpansion

REM Resolve project root and source path.
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"
if errorlevel 1 exit /b 1
set "PYTHONPATH=%ROOT_DIR%src"
set "APP_SCRIPT=%ROOT_DIR%web\chanlun_chart\app.py"

if exist "%APP_SCRIPT%" goto :resolve_python
echo ERROR: application script not found: %APP_SCRIPT%
exit /b 1

:resolve_python
REM app.py loads project .env before resolving CHANLUN_WEB_HOST and
REM CHANLUN_WEB_PORT. Do not set launcher defaults that mask .env values.
REM Python resolution matches ops/restart_qmt_daily.ps1:
REM CHANLUN_PYTHON, then .venv, then the Poetry environment.
if defined CHANLUN_PYTHON goto :run_configured_python
if exist "%ROOT_DIR%.venv\Scripts\python.exe" goto :run_venv_python
where poetry >nul 2>&1
if errorlevel 1 goto :missing_python
poetry run python "%APP_SCRIPT%"
exit /b %ERRORLEVEL%

:run_configured_python
if exist "%CHANLUN_PYTHON%" goto :configured_python_exists
echo ERROR: CHANLUN_PYTHON does not exist: %CHANLUN_PYTHON%
exit /b 1

:configured_python_exists
"%CHANLUN_PYTHON%" "%APP_SCRIPT%"
exit /b %ERRORLEVEL%

:run_venv_python
"%ROOT_DIR%.venv\Scripts\python.exe" "%APP_SCRIPT%"
exit /b %ERRORLEVEL%

:missing_python
echo ERROR: Poetry was not found and no project Python is configured.
exit /b 1