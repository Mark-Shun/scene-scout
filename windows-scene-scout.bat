@echo off
setlocal

:: Teleport to the script's actual directory
cd /d "%~dp0"

:: Check for custom environment path from install state
set "CUSTOM_ENV_PATH="
if exist ".install_state" (
    for /f "tokens=1,* delims==" %%A in (.install_state) do (
        if "%%A"=="ENV_PATH" set "CUSTOM_ENV_PATH=%%B"
    )
)
if defined CUSTOM_ENV_PATH (
    set "UV_PROJECT_ENVIRONMENT=%CUSTOM_ENV_PATH%\.venv"
)

set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"

"%UV_EXE%" run --no-sync src\scenescout.py

if errorlevel 1 (
    pause
    exit /b 1
)