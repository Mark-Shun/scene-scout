@echo off
setlocal

:: Teleport to the script's actual directory
cd /d "%~dp0"

set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"

"%UV_EXE%" run --no-sync src\scenescout.py

if errorlevel 1 (
    pause
    exit /b 1
)