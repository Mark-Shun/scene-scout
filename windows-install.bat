@echo off
setlocal

:: Teleport to the script's actual directory
cd /d "%~dp0"

set "LOGO_ASCII=assets\logo\logo.txt"

cls
if exist "%LOGO_ASCII%" (
    type "%LOGO_ASCII%"
) else (
    echo [!] logo.txt not found.
)
echo.
echo ---Windows installation script for Scene Scout---

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

:: --- Backend Acceleration Installation ---
:MENU
echo ------------------------------------------
echo Detecting GPU hardware...
set "SUGGEST_OPT=5"
set "SUGGEST_NAME=5 (CPU)"
set "GPU_FOUND="

for /f "delims=" %%I in ('powershell -NoProfile -Command "(Get-CimInstance Win32_VideoController).Name" 2^>nul') do (
    echo %%I | findstr /i "NVIDIA" >nul
    if not errorlevel 1 (
        set "GPU_FOUND=NVIDIA"
        
        :: Check nvidia-smi for supported CUDA version
        nvidia-smi >nul 2>&1
        if not errorlevel 1 (
            nvidia-smi | findstr /c:"CUDA Version: 13" >nul
            if not errorlevel 1 (
                set "SUGGEST_OPT=1"
                set "SUGGEST_NAME=1 (NVIDIA CUDA 13.0)"
            ) else (
                set "SUGGEST_OPT=2"
                set "SUGGEST_NAME=2 (NVIDIA CUDA 12.6)"
            )
        ) else (
            :: Fallback if nvidia-smi is unavailable
            set "SUGGEST_OPT=1"
            set "SUGGEST_NAME=1 (NVIDIA CUDA - Auto-detect failed)"
            echo [!] NVIDIA GPU detected, but driver version check failed. Please verify your selection manually.
        )
    )
    echo %%I | findstr /i "AMD" >nul
    if not errorlevel 1 if not defined GPU_FOUND (
        set "SUGGEST_OPT=3"
        set "SUGGEST_NAME=3 (DirectML)"
        set "GPU_FOUND=AMD"
    )
    echo %%I | findstr /i "Intel" >nul
        if not errorlevel 1 if not defined GPU_FOUND (
            :: Refine check to only catch Arc or Xe graphics, ignoring generic UHD/HD
            echo %%I | findstr /i "Arc Xe" >nul
            if not errorlevel 1 (
                set "SUGGEST_OPT=4"
                set "SUGGEST_NAME=4 (Intel Arc/Xe)"
                set "GPU_FOUND=Intel Arc/Xe"
            )
        )

if defined GPU_FOUND (
    echo [Detected %GPU_FOUND% GPU]
) else (
    echo [No dedicated GPU recognized - Defaulting to CPU]
)
echo ------------------------------------------
echo Install options for graphics card acceleration:
echo 1) NVIDIA CUDA 13.0 (RTX, newer GPUs)
echo 2) NVIDIA CUDA 12.6 (GTX, older GPUs)
echo 3) DirectML (AMD/Intel or Nvidia GPU, Windows only)
echo 4) Intel Arc/Xe (XPU)
echo 5) CPU (Slow)
echo ------------------------------------------

set /p "user_choice=Select an option [1-5] (Press Enter for Default: %SUGGEST_NAME%): "
if "%user_choice%"=="" set "user_choice=%SUGGEST_OPT%"

if "%user_choice%"=="1" goto :TRT_PROMPT
goto :PROCEED_NORMAL

:TRT_PROMPT
echo.
echo TensorRT can significantly speed up search on NVIDIA GPUs.
echo Note: This requires an extra ~1GB download and dynamic initial compile time for search and index.
choice /C YN /N /M "Would you like to install with TensorRT optimization? [Y/n]: "
if errorlevel 2 (
    set "EXTRA=cu130"
) else (
    set "EXTRA=cu130-trt"
)
goto :CHECK_OPTION

:PROCEED_NORMAL
if "%user_choice%"=="2" set "EXTRA=cu126"
if "%user_choice%"=="3" (
    set "EXTRA=dml"
    set "FLAGS=--prerelease=allow"
    set "PY_VER=--python 3.10"
)
if "%user_choice%"=="4" set "EXTRA=xpu"
if "%user_choice%"=="5" set "EXTRA=cpu"

:CHECK_OPTION
if "%EXTRA%"=="" (
    echo Error: Invalid selection. 
    pause 
    exit /b 1 
)

:: --- Read existing optional paths from previous installs ---
set "OLD_ENV_PATH="
set "OLD_HF_HOME="
if exist "%BASE_DIR%.install_state" (
    for /f "tokens=1,* delims==" %%A in (%BASE_DIR%.install_state) do (
        if "%%A"=="ENV_PATH" set "OLD_ENV_PATH=%%B"
        if "%%A"=="HF_HOME" set "OLD_HF_HOME=%%B"
    )
)

:: --- Initialize Install State (Overwrites old file) ---
echo EXTRA=%EXTRA%> "%BASE_DIR%.install_state"
if not "%FLAGS%"=="" echo FLAGS=%FLAGS%>> "%BASE_DIR%.install_state"
if not "%PY_VER%"=="" echo PY_VER=%PY_VER%>> "%BASE_DIR%.install_state"

:: --- Custom Environment Path Setup ---
echo.
set "CUSTOM_ENV_PATH="
if defined OLD_ENV_PATH (
    choice /C KC /N /M "App files: %OLD_ENV_PATH% - (K)eep / (C)hange? [K/c]: "
    if errorlevel 2 goto PROMPT_ENV_PATH
    set "CUSTOM_ENV_PATH=%OLD_ENV_PATH%"
    goto ENV_PATH_DONE
)

choice /C YN /N /M "Use custom folder for app files? [y/N]: "
if errorlevel 2 goto ENV_PATH_DONE

:PROMPT_ENV_PATH
set /p "CUSTOM_ENV_PATH=Enter full absolute path (e.g., D:\scout_env): "
set "CUSTOM_ENV_PATH=%CUSTOM_ENV_PATH:"=%"

:: Validate Path
set "PARENT_DIR=%~dp0"
for %%I in ("%CUSTOM_ENV_PATH%") do set "PARENT_DIR=%%~dpI"

if not exist "%PARENT_DIR%" (
    echo [!] The parent folder does not exist. Try again.
    goto PROMPT_ENV_PATH
)

mkdir "%CUSTOM_ENV_PATH%" 2>nul
if exist "%CUSTOM_ENV_PATH%" (
    echo [SUCCESS] App files will be installed to: %CUSTOM_ENV_PATH%
) else (
    echo [!] Access Denied. Falling back to installation in scene scout folder.
    set "CUSTOM_ENV_PATH="
)

:ENV_PATH_DONE

:: --- HuggingFace Cache Path Setup ---
set "CUSTOM_HF_HOME="
if defined OLD_HF_HOME (
    choice /C KC /N /M "AI models: %OLD_HF_HOME% - (K)eep / (C)hange? [K/c]: "
    if errorlevel 2 goto PROMPT_HF_HOME
    set "CUSTOM_HF_HOME=%OLD_HF_HOME%"
    goto HF_HOME_DONE
)

choice /C YN /N /M "Use custom folder for AI models? [y/N]: "
if errorlevel 2 goto HF_HOME_DONE

:PROMPT_HF_HOME
set /p "CUSTOM_HF_HOME=Enter full absolute path (e.g., D:\scout_cache\hf): "
set "CUSTOM_HF_HOME=%CUSTOM_HF_HOME:"=%"

set "PARENT_DIR=%~dp0"
for %%I in ("%CUSTOM_HF_HOME%") do set "PARENT_DIR=%%~dpI"

if not exist "%PARENT_DIR%" (
    echo [!] The parent folder does not exist. Try again.
    goto PROMPT_HF_HOME
)

mkdir "%CUSTOM_HF_HOME%" 2>nul
if exist "%CUSTOM_HF_HOME%" (
    echo [SUCCESS] AI models cache will be set to: %CUSTOM_HF_HOME%
) else (
    echo [!] Access Denied. Falling back to default cache location.
    set "CUSTOM_HF_HOME="
)

:HF_HOME_DONE
echo.
echo ------------------------------------------

:: Write optional paths to install state
if defined CUSTOM_ENV_PATH echo ENV_PATH=%CUSTOM_ENV_PATH%>> "%BASE_DIR%.install_state"
if defined CUSTOM_HF_HOME echo HF_HOME=%CUSTOM_HF_HOME%>> "%BASE_DIR%.install_state"

:: --- Installation Mode Selection ---
:: Determine where the virtual environment currently lives
set "ACTUAL_ENV_PATH=%~dp0.venv"
if defined CUSTOM_ENV_PATH (
    set "ACTUAL_ENV_PATH=%CUSTOM_ENV_PATH%\.venv"
    set "UV_PROJECT_ENVIRONMENT=%CUSTOM_ENV_PATH%\.venv"
)

:: If an environment already exists, ask the user how to handle it
if exist "%ACTUAL_ENV_PATH%" (
    echo.
    echo ==========================================
    echo Existing Python environment detected.
    echo [1] Standard Update (Fast - updates modified packages only)
    echo [2] Clean Install (Fixes corrupted environments and broken dependencies)
    echo ==========================================
    choice /C 12 /M "Select installation mode [1 (Fast) / 2 (Clean)]:"
    if errorlevel 2 (
        echo.
        echo Wiping old environment...
        rmdir /s /q "%ACTUAL_ENV_PATH%"
        echo Old environment removed. Proceeding with clean install.
    )
)

:: --- Actual installation ---

echo.

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
choice /C YN /N /M "Would you like to launch %NAME% now? [y/N]: "
:: If 'N' is chosen (errorlevel 2), the script will simply exit.
if errorlevel 2 exit

:: If 'Y' is chosen (errorlevel 1), start the batch file in a new process and close this terminal.
start "" "%TARGET_PATH%"
exit
