@echo off
setlocal EnableExtensions DisableDelayedExpansion

REM Use the managed restart path so readiness, revision and port ownership are
REM verified before the browser opens. A normal double-click is always bounded.
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"
if errorlevel 1 exit /b 1

set "RESTART_SCRIPT=%ROOT_DIR%ops\restart_web.ps1"
if exist "%RESTART_SCRIPT%" goto :restart
echo ERROR: managed restart script not found: %RESTART_SCRIPT%
exit /b 1

:restart
REM This is the user-facing production launcher. The PowerShell script still
REM defaults to the 12-symbol validation cohort when invoked without switches,
REM so code changes can be verified quickly without rebuilding the full market.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RESTART_SCRIPT%" -EnableLargeScreeningScope -EnableLargeHoldingMonitorScope -EnableFullSymbolCatalog -EnableFullCoverage -ForceFullCoverageUntilComplete -OpenBrowser
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo ERROR: managed web restart failed with exit code %EXIT_CODE%.
exit /b %EXIT_CODE%
