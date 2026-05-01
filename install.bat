@echo off
setlocal

echo ---Installation script for Scene Scout---

:: Define the local folder 
set "UV_DIR=%~dp0.uv" 
set "UV_EXE=%UV_DIR%\uv.exe" 
set "UV_PYTHON_INSTALL_DIR=%UV_DIR%\python" 
set "UV_CACHE_DIR=%UV_DIR%\uv_cache" 

:: Install uv locally if missing 
if not exist "%UV_EXE%" (
    echo Downloading uv to isolated folder... 
    if not exist "%UV_DIR%" mkdir "%UV_DIR%" 
    powershell -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_DIR%'; $env:UV_UNMANAGED_INSTALL='1'; irm https://astral.sh/uv/install.ps1 | iex" 
)

:: Add the isolated folder to this session's PATH 
set "PATH=%UV_DIR%;%PATH%" 

:: Check for VLC Media Player
echo Checking for VLC...
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" (
    echo VLC is already installed. 
    goto :MENU
)

echo VLC was not found. This application requires VLC for the scene playback viewer.
choice /C YN /M "Would you like to install VLC via winget now?"

:: CHOICE sets errorlevel: 1 for Y, 2 for N
:: Note: 'if errorlevel' checks if value is >= the number. Check 2 first.
if errorlevel 2 (
    echo Skipping automatic VLC installation. Please install VLC manually at: https://www.videolan.org/ [cite: 4, 5]
    pause 
    exit /b 1 
)

echo Attempting to install VLC...
winget install --id VideoLAN.VLC --silent --accept-source-agreements --accept-package-agreements

if errorlevel 1 (
    echo [!] Automatic installation failed. Please install VLC manually at: https://www.videolan.org/ 
    pause 
    exit /b 1 
)

echo VLC installed successfully.

:MENU
:: Interactive Menu
echo ------------------------------------------
echo Install options:
echo 1) NVIDIA CUDA 13.0 (RTX, newer GPUs)
echo 2) NVIDIA CUDA 12.6 (GTX, older GPUs)
echo 3) DirectML (AMD/Intel or Nvidia GPU, Windows only and a bit slower than native)
echo 4) Intel Arc/Xe (XPU)
echo 5) AMD ROCm (Note: Linux only)
echo 6) CPU (Slow)
echo ------------------------------------------

set /p user_choice="Select an option [1-6]: "

:: Map choices to uv extras
if "%user_choice%"=="1" set "EXTRA=cu130"
if "%user_choice%"=="2" set "EXTRA=cu126"
if "%user_choice%"=="3" (
    set "EXTRA=dml"
    set "FLAGS=--prerelease=allow"
    :: Force 3.12 only for DirectML to satisfy Torch 2.4.1 requirements
    set "PY_VER=--python 3.12"
)
if "%user_choice%"=="4" set "EXTRA=xpu"
if "%user_choice%"=="5" set "EXTRA=rocm"
if "%user_choice%"=="6" set "EXTRA=cpu"

:: Validation check 
if "%EXTRA%"=="" (
    echo Error: Invalid selection. 
    pause 
    exit /b 1 
)

echo Running installer with extra: %EXTRA%...
uv sync --extra %EXTRA% %FLAGS% %PY_VER%

:: Check if the previous command failed (errorlevel >= 1)
if errorlevel 1 (
    echo.
    echo [!] Installation failed. Please check the error message above.
    pause
    exit /b 1
)

echo Installation succesful, you can now open run.bat
pause