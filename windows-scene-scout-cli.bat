@echo off
setlocal
set "UV_DIR=%~dp0.uv"
set "UV_EXE=%UV_DIR%\uv.exe"

:: %* passes any arguments given to the .bat file directly to the python script
"%UV_EXE%" run --no-sync src\scenescout.py --interactive %*

if errorlevel 1 (
    pause
    exit /b 1
)