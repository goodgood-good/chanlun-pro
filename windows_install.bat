@echo off
setlocal enabledelayedexpansion

REM ��ȡ�ű�����Ŀ¼����Ŀ��Ŀ¼��
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

REM ROOT_DIR ��ֵ
echo ROOT_DIR: %ROOT_DIR%

echo 1. uv Ŀ¼
set "UV_DIR=%ROOT_DIR%script\bin\uv.exe"
echo UV_DIR: %UV_DIR%

echo 2. �������⻷��
%UV_DIR% python install 3.11
%UV_DIR% venv --python=3.11 .venv
%UV_DIR% sync
REM Default install: core deps only. Optional extras (one or more):
REM   us / hk / cn-extra / futures / ai / notify / backtest / monitor
REM Examples:
REM   %UV_DIR% sync --extra us --extra hk
REM   %UV_DIR% sync --all-extras

echo 3. ��������ļ�
if not exist "%ROOT_DIR%src\chanlun\config.py" (
    echo ���������ļ�...
    copy "%ROOT_DIR%src\chanlun\config.py.demo" "%ROOT_DIR%src\chanlun\config.py" >nul
)

echo 4. ���û�������
set "PYTHONPATH=%ROOT_DIR%src"
echo ����PYTHONPATH: !PYTHONPATH!

echo 5. ���л������ű�
if exist "%ROOT_DIR%check_env.py" (
    %UV_DIR% run "%ROOT_DIR%check_env.py"
)

echo ����������ɣ�
pause
