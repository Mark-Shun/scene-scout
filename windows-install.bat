@echo off
setlocal

set "LOGO_ASCII=assets\logo\logo.txt"

cls
if exist "%LOGO_ASCII%" (
    type "%LOGO_ASCII%"
) else (
    echo [!] logo.txt not found.
)
echo.
echo ---Windows installation script for Scene Scout---

:: Teleport to the script's actual directory
cd /d "%~dp0"

:: --- START UPDATE CHECK ---
echo Checking for updates...
powershell -ExecutionPolicy Bypass -Command "try { $v = (Select-String -Path '%~dp0pyproject.toml' -Pattern '^version = \"(.*)\"').Matches.Groups[1].Value; $latest = (Invoke-RestMethod -Uri 'https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest' -TimeoutSec 2).tag_name.TrimStart('v'); if ([version]$latest -gt [version]$v) { Write-Host \"`n[UPDATE] A newer version (v$latest) is available!\" -ForegroundColor Cyan; Write-Host 'Latest Release: https://github.com/Mark-Shun/scene-scout/releases/latest' -ForegroundColor White; Write-Host \"Current version: v$v`n\" -ForegroundColor Gray } } catch {}"
:: --- END UPDATE CHECK ---

:: Define the local folders 
set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%UV_DIR%\python"
set "UV_CACHE_DIR=%UV_DIR%\uv_cache"

:: Set UV options 
set "UV_VENV_CLEAR=1"

:: Set shortcut variables 
set "NAME=Scene Scout"
set "TARGET=windows-scene-scout.bat"
set "ICON=assets\logo\scene-scout-logo.ico"
set "BASE_DIR=%~dp0"
set "TARGET_PATH=%BASE_DIR%%TARGET%"
set "ICON_PATH=%BASE_DIR%%ICON%"
set "SHORTCUT_PATH=%BASE_DIR%%NAME%.lnk"

for /f "delims=" %%i in ('powershell -command "[Environment]::GetFolderPath('Desktop')"') do set "DESKTOP_DIR=%%i"
set "DESKTOP_SHORTCUT_PATH=%DESKTOP_DIR%\%NAME%.lnk"

:: Install uv locally if missing 
if not exist "%UV_EXE%" (
    echo Downloading uv to isolated folder... 
    if not exist "%UV_DIR%" mkdir "%UV_DIR%" 
    powershell -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_DIR%'; $env:UV_UNMANAGED_INSTALL='1'; irm https://astral.sh/uv/install.ps1 | iex" 
)

:: Add the isolated folder to this session's PATH 
set "PATH=%UV_DIR%;%PATH%" 

:: Check for VLC Media Player
echo.
echo Checking for VLC...
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe" (
    echo VLC is already installed. 
    goto :MENU
)

echo VLC was not found. This application requires VLC for the scene playback viewer.
choice /C YN /M "Would you like to install VLC via winget now?" 

if errorlevel 2 (
    echo Skipping automatic VLC installation. Please install VLC manually at: https://www.videolan.org/
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
echo.

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
goto :PROCEED_NORMAL

:TRT_PROMPT
echo.
echo TensorRT can significantly speed up search on NVIDIA GPUs.
echo Note: This requires an extra ~1GB download and dynamic initial compile time for search and index. 
choice /C YN /M "Would you like to install with TensorRT optimization?" 
if errorlevel 2 (
    set "EXTRA=cu130"
) else (
    set "EXTRA=cu130-trt"
)
goto :INSTALL_START

:PROCEED_NORMAL
if "%user_choice%"=="2" set "EXTRA=cu126"
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

echo EXTRA=%EXTRA%> "%BASE_DIR%.install_state"
if not "%FLAGS%"=="" echo FLAGS=%FLAGS%>> "%BASE_DIR%.install_state"
if not "%PY_VER%"=="" echo PY_VER=%PY_VER%>> "%BASE_DIR%.install_state"

echo Running installer with extra: %EXTRA%...
"%UV_EXE%" sync --extra %EXTRA% %FLAGS% %PY_VER% 

if errorlevel 1 (
    echo.
    echo [!] Installation failed. Please check the error message above. 
    pause
    exit /b 1
)

echo.
echo Checking and creating shortcuts for %NAME%... 

:: Local Folder Shortcut Check & Creation
if not exist "%SHORTCUT_PATH%" (
    powershell -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath='%TARGET_PATH%'; $s.WorkingDirectory='%BASE_DIR%'; $s.IconLocation='%ICON_PATH%'; $s.Save()"
    if exist "%SHORTCUT_PATH%" (
        powershell -Command "Write-Host '[SUCCESS] Local shortcut created.' -ForegroundColor Green"
    )
)

:: Desktop Shortcut Check & Creation
if not exist "%DESKTOP_SHORTCUT_PATH%" (
    powershell -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%DESKTOP_SHORTCUT_PATH%'); $s.TargetPath='%TARGET_PATH%'; $s.WorkingDirectory='%BASE_DIR%'; $s.IconLocation='%ICON_PATH%'; $s.Save()"
    if exist "%DESKTOP_SHORTCUT_PATH%" (
        powershell -Command "Write-Host '[SUCCESS] Desktop shortcut created.' -ForegroundColor Green"
    )
)

:EXIT_PROMPT
echo.
choice /C YN /M "Would you like to launch %NAME% now?"

:: If 'N' is chosen (errorlevel 2), the script will simply exit.
if errorlevel 2 exit

:: If 'Y' is chosen (errorlevel 1), start the batch file in a new process and close this terminal.
start "" "%TARGET_PATH%"
exit
