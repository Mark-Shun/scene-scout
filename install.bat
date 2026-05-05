@echo off
setlocal enabledelayedexpansion

echo ---Installation script for Scene Scout---

:: Define the local folder 
set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%UV_DIR%\python"
set "UV_CACHE_DIR=%UV_DIR%\uv_cache"

:: Set UV options
set "UV_VENV_CLEAR=1"

:: 1. Install uv locally if missing
if not exist "%UV_EXE%" (
    echo Downloading uv to isolated folder...
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    
    powershell -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_DIR%'; $env:UV_UNMANAGED_INSTALL='1'; irm https://astral.sh/uv/install.ps1 | iex"
    
    if not exist "%UV_EXE%" (
        echo PowerShell install failed. Attempting direct download...
        curl -L "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -o "%UV_DIR%\uv.zip"
        powershell -Command "Expand-Archive -Path '%UV_DIR%\uv.zip' -DestinationPath '%UV_DIR%' -Force"
        move /y "%UV_DIR%\uv-x86_64-pc-windows-msvc\uv.exe" "%UV_EXE%"
        del "%UV_DIR%\uv.zip"
        rmdir /s /q "%UV_DIR%\uv-x86_64-pc-windows-msvc"
    )
)

if not exist "%UV_EXE%" (
    echo [!] Error: Could not install uv. An internet connection is required.
    pause
    exit /b 1
)

set "PATH=%UV_DIR%;%PATH%"

:: 2. VLC Requirement Check with CLI Fallback
set "CLI_ONLY=0"
echo Checking for VLC...
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" (
    echo VLC is already installed.
    goto :MENU
)

echo VLC was not found. The GUI requires VLC for video playback.
choice /C YN /M "Would you like to attempt to install VLC now?"

if errorlevel 2 (
    echo Proceeding with CLI-only support.
    set "CLI_ONLY=1"
    goto :MENU
)

echo Attempting to install VLC via winget...
winget install --id VideoLAN.VLC --silent --accept-source-agreements --accept-package-agreements

if errorlevel 1 (
    echo [!] Winget failed. Attempting direct installer download...
    curl -L "https://get.videolan.org/vlc/last/win64/vlc-3.0.21-win64.exe" -o "%temp%\vlc_setup.exe"
    if exist "%temp%\vlc_setup.exe" (
        echo Running silent installer...
        start /wait "" "%temp%\vlc_setup.exe" /S
        del "%temp%\vlc_setup.exe"
    )
)

:: Final check to see if VLC installation actually succeeded
if not exist "C:\Program Files\VideoLAN\VLC\vlc.exe" (
    echo [!] VLC could not be installed. 
    echo Installation will continue, but only the CLI will be functional.
    set "CLI_ONLY=1"
    pause
) else (
    echo VLC installed successfully.
)

:MENU
echo ------------------------------------------
echo Install options for graphics card acceleration:
echo 1) NVIDIA CUDA 13.0 (RTX, newer GPUs)
echo 2) NVIDIA CUDA 12.6 (GTX, older GPUs)
echo 3) DirectML (AMD/Intel or Nvidia GPU, Windows only)
echo 4) Intel Arc/Xe (XPU)
echo 5) CPU (Slow)
echo ------------------------------------------

set /p user_choice="Select an option [1-5]: "

if "%user_choice%"=="1" goto :TRT_PROMPT
if "%user_choice%"=="2" goto :TRT_PROMPT
goto :PROCEED_NORMAL

:TRT_PROMPT
echo.
echo TensorRT can significantly speed up search on NVIDIA GPUs.
choice /C YN /M "Would you like to install with TensorRT optimization? "
if errorlevel 2 (
    if "%user_choice%"=="1" set "EXTRA=cu130"
    if "%user_choice%"=="2" set "EXTRA=cu126"
) else (
    if "%user_choice%"=="1" set "EXTRA=cu130-trt"
    if "%user_choice%"=="2" set "EXTRA=cu126-trt"
)
goto :INSTALL_START

:PROCEED_NORMAL
if "%user_choice%"=="3" (
    set "EXTRA=dml"
    set "FLAGS=--prerelease=allow"
    set "PY_VER=--python 3.12"
)
if "%user_choice%"=="4" set "EXTRA=xpu"
if "%user_choice%"=="5" set "EXTRA=cpu"

:INSTALL_START
if "%EXTRA%"=="" (
    echo Error: Invalid selection.
    pause 
    exit /b 1 
)

echo Running installer with extra: %EXTRA%...
uv venv
uv pip install -e .[%EXTRA%] %FLAGS% %PY_VER%

if errorlevel 1 (
    echo.
    echo [!] Installation failed.
    pause
    exit /b 1
)

echo --------------------------------------------------
echo Installation successful.
if "%CLI_ONLY%"=="1" (
    echo NOTICE: VLC is missing. Use the CLI via run_cli.bat file or manually with uv run --no-sync src/scenescout.py --interactive
    echo The 'run_gui.bat' file will not function correctly without VLC.
) else (
    echo You can now open 'run_gui.bat'.
)
echo --------------------------------------------------
pause