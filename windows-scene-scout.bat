@echo off
setlocal

:: Teleport to the script's actual directory
cd /d "%~dp0"

:: Check if installer has been run
if not exist ".install_state" (
    echo [!] Installation state not found. The application may not be installed.
    choice /C YN /M "Would you like to run the installer now?"
    if errorlevel 2 exit /b 1
    call "windows-install.bat"
    exit /b
)

:: Check for custom environment, HuggingFace cache paths, and extras from install state
set "CUSTOM_ENV_PATH="
set "CUSTOM_HF_HOME="
set "INSTALL_EXTRA="
if exist ".install_state" (
    for /f "tokens=1,* delims==" %%A in (.install_state) do (
        if "%%A"=="ENV_PATH" set "CUSTOM_ENV_PATH=%%B"
        if "%%A"=="HF_HOME" set "CUSTOM_HF_HOME=%%B"
        if "%%A"=="EXTRA" set "INSTALL_EXTRA=--extra %%B"
    )
)
if defined CUSTOM_ENV_PATH (
    set "UV_PROJECT_ENVIRONMENT=%CUSTOM_ENV_PATH%\.venv"
)
if defined CUSTOM_HF_HOME (
    set "HF_HOME=%CUSTOM_HF_HOME%"
)

set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"

"%UV_EXE%" run %INSTALL_EXTRA% src\scenescout.py

if errorlevel 1 (
    pause
    exit /b 1
)