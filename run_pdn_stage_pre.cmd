@echo off
setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "VENV_PY=c:\venvs\pyaedt\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [ERROR] Python not found: %VENV_PY%
  exit /b 1
)

cd /d "%ROOT_DIR%"
"%VENV_PY%" "%ROOT_DIR%pdn_automation.py" --stage pre
exit /b %ERRORLEVEL%
