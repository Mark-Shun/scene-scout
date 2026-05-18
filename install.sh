#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# --- START UPDATE CHECK ---
echo "Checking for updates..."
REMOTE_TAG=$(curl -s --connect-timeout 2 https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest | grep '"tag_name":' | sed -E 's/.*"v?([^"]+)".*/\1/')
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

check_tcl_tk() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        [ -d "/opt/homebrew/opt/tcl-tk" ] || [ -d "/opt/homebrew/opt/tcl-tk@8" ] || [ -d "/usr/local/opt/tcl-tk@8" ]
    else
        command -v wish >/dev/null 2>&1
    fi
}

ensure_homebrew() {
    if [[ "$OSTYPE" == "darwin"* ]] && ! command -v brew >/dev/null 2>&1; then
        echo "Homebrew not found. Attempting to install automatically..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        
        if [ -d "/opt/homebrew/bin" ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -d "/usr/local/bin" ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi
}

install_vlc() {
    echo "Installing VLC..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        ensure_homebrew
        brew install --cask vlc
    elif [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|linuxmint) sudo apt update && sudo apt install -y vlc ;;
            arch|manjaro) sudo pacman -S --needed --noconfirm vlc ;;
            *) echo "Unsupported Linux distribution for automatic install."; return 1 ;;
        esac
    fi
}

install_tcl_tk() {
    echo "Installing Tcl/Tk..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        ensure_homebrew
        ARCH=$(uname -m)
        if [ "$ARCH" = "x86_64" ]; then
            brew install tcl-tk@8
        else
            brew install tcl-tk
        fi
    elif [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|linuxmint) sudo apt update && sudo apt install -y python3-tk ;;
            arch|manjaro) sudo pacman -S --needed --noconfirm tk ;;
            *) echo "Unsupported Linux distribution for automatic install."; return 1 ;;
        esac
    fi
}

echo "Checking system GUI dependencies..."

if ! check_vlc; then
    echo "VLC was not found. The GUI requires VLC for video playback."
    read -p "Install VLC automatically? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_vlc
    fi
else
    echo "VLC is already installed."
fi

if ! check_tcl_tk; then
    echo "Tcl/Tk framework was not found. The GUI requires Tcl/Tk to render windows."
    read -p "Install Tcl/Tk automatically? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_tcl_tk
    fi
else
    echo "Tcl/Tk framework is already installed."
fi

if ! check_vlc || ! check_tcl_tk; then
    echo "[!] Missing critical components. Continuing in CLI-only mode."
    CLI_ONLY=1
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

# 4. Final Permissions and Cleanup
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' 's/\r//' "$SCRIPT_DIR/scene-scout.command" 2>/dev/null
    chmod +x "$SCRIPT_DIR/scene-scout.command"
    xattr -cr "$SCRIPT_DIR" 2>/dev/null
    
    if [ "$CLI_ONLY" -eq 0 ]; then
        echo "Run via: ./scene-scout.command"
    fi
else
    chmod +x "$SCRIPT_DIR/scene-scout.sh"
    
    if [ "$CLI_ONLY" -eq 0 ]; then
        echo "Run via: ./scene-scout.sh"
    fi
fi