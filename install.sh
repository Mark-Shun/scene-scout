#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# --- START UPDATE CHECK ---
echo "Checking for updates..."
# Fetch remote tag and strip 'v' prefix if present
REMOTE_TAG=$(curl -s --connect-timeout 2 https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest | grep '"tag_name":' | sed -E 's/.*"v?([^"]+)".*/\1/')
# Read local version from pyproject.toml
LOCAL_VER=$(grep '^version =' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')

if [ -n "$REMOTE_TAG" ] && [ "$REMOTE_TAG" != "$LOCAL_VER" ]; then
    echo -e "\n\033[1;36m[UPDATE] A newer version (v$REMOTE_TAG) is available!\033[0m"
    echo -e "\033[1;37mLatest Release: https://github.com/Mark-Shun/scene-scout/releases/latest\033[0m"
    echo -e "\033[0;90mCurrent version: v$LOCAL_VER\033[0m\n"
fi
# --- END UPDATE CHECK ---

# Set uv environment
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 
export UV_PYTHON_INSTALL_DIR="$UV_DIR/python" 
export UV_CACHE_DIR="$UV_DIR/uv_cache"
export UV_VENV_CLEAR=1

# 1. Install uv locally if missing
if [ ! -f "$UV_EXE" ]; then
    echo "Downloading uv to isolated folder..." 
    mkdir -p "$UV_DIR" 
    export UV_INSTALL_DIR="$UV_DIR"
    export UV_UNMANAGED_INSTALL="1"
    curl -LsSf https://astral.sh/uv/install.sh | sh 
fi

export PATH="$UV_DIR:$PATH" 

# 2. Dependency Check Logic
CLI_ONLY=0

check_vlc() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        [ -d "/Applications/VLC.app" ] || command -v vlc >/dev/null 2>&1
    else
        command -v vlc >/dev/null 2>&1
    fi
}

install_dependencies() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "Detected macOS..."
        if command -v brew >/dev/null 2>&1; then
            echo "Installing VLC and Tcl/Tk via Homebrew..."
            brew install --cask vlc && brew install tcl-tk
        else
            echo "[!] Homebrew not found. Automated install failed."
            return 1
        fi
    elif [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|linuxmint)
                sudo apt update && sudo apt install -y python3-tk vlc ;;
            arch|manjaro)
                sudo pacman -S --needed --noconfirm tk vlc ;;
            *) return 1 ;;
        esac
    fi
}

echo "Checking for VLC..."
if check_vlc; then
    echo "VLC is already installed."
else
    echo "VLC was not found. The GUI requires VLC for video playback."
    read -p "Attempt to install dependencies automatically? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_dependencies
        if ! check_vlc; then
            echo "[!] Automatic installation failed. Continuing in CLI-only mode."
            CLI_ONLY=1
        fi
    else
        CLI_ONLY=1
    fi
fi

# 3. Hardware Selection
echo "------------------------------------------"
echo "Install options for graphics card acceleration:"

if [[ "$OSTYPE" == "darwin"* ]]; then
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        echo "Intel Mac detected. Automatically selecting compatible CPU fallback..."
        EXTRA="mac-intel"
    else
        echo "Apple Silicon Mac detected. Automatically selecting native MPS support..."
        EXTRA="cpu"
    fi
else
    echo "1) NVIDIA CUDA 13.0"
    echo "2) NVIDIA CUDA 12.6"
    echo "3) Intel Arc/Xe (XPU)" 
    echo "4) AMD ROCm"
    echo "5) CPU (Slow)"
    echo "------------------------------------------"
    read -p "Select an option [1-5]: " user_choice
    case "$user_choice" in
        1) EXTRA="cu130" ;;
        2) EXTRA="cu126" ;;
        3) EXTRA="xpu" ;;
        4) EXTRA="rocm" ;;
        5) EXTRA="cpu" ;;
        *) echo "Error: Invalid selection."; exit 1 ;;
    esac
fi

echo "Synchronizing environment with extra: $EXTRA..."
# uv sync ensures the environment matches pyproject.toml
if uv sync --extra "$EXTRA" --python 3.12; then
    echo "--------------------------------------------------"
    echo "Installation successful."
    if [ "$CLI_ONLY" -eq 1 ]; then
        echo "NOTICE: VLC/Tkinter missing. Only CLI mode is supported."
        echo "You can install these manually if you want to use the GUI."
        echo "Run via: ./scene-scout-cli.sh"
    fi
    echo "--------------------------------------------------"
else
    echo "Error: Synchronization failed."
    exit 1
fi

if [[ "$OSTYPE" == "darwin"* ]]; then
    chmod +x "$SCRIPT_DIR/scene-scout.command"
    
    # Strip the quarantine flag recursively from the entire project folder
    xattr -cr "$SCRIPT_DIR" 2>/dev/null
    
    echo "Run via: ./scene-scout.command"
else
    chmod +x "$SCRIPT_DIR/scene-scout.sh"
    echo "Run via: ./scene-scout.sh"
fi