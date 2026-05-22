#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# --- START UPDATE CHECK ---
echo "Checking for updates..."
REMOTE_TAG=$(curl -s --connect-timeout 2 https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest | grep '"tag_name":' | sed -E 's/.*"v?([^"]+)".*/\1/')
LOCAL_VER=$(grep '^version =' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')

if [ -n "$REMOTE_TAG" ] && [ -n "$LOCAL_VER" ]; then
    # Sort the two versions; if the lower version matches local, remote must be newer
    LOWER_VER=$(printf '%s\n%s' "$LOCAL_VER" "$REMOTE_TAG" | sort -V | head -n 1)
    
    if [ "$LOCAL_VER" != "$REMOTE_TAG" ] && [ "$LOWER_VER" = "$LOCAL_VER" ]; then
        echo -e "\n\033[1;36m[UPDATE] A newer version (v$REMOTE_TAG) is available!\033[0m"
        echo -e "\033[1;37mLatest Release: https://github.com/Mark-Shun/scene-scout/releases/latest\033[0m"
        echo -e "\033[0;90mCurrent version: v$LOCAL_VER\033[0m\n"
    fi
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
    command -v vlc >/dev/null 2>&1
}

install_vlc() {
    echo "Installing VLC..."
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|linuxmint) sudo apt update && sudo apt install -y vlc ;;
            arch|manjaro) sudo pacman -S --needed --noconfirm vlc ;;
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

if ! check_vlc; then
    echo "[!] Missing critical components. Continuing in CLI-only mode."
    CLI_ONLY=1
fi

# 3. Hardware Selection
echo "------------------------------------------"
echo "Install options for graphics card acceleration:"
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

echo "Synchronizing environment with extra: $EXTRA..."
if uv sync --extra "$EXTRA" --python 3.12; then
    echo "--------------------------------------------------"
    echo "Installation successful."
    if [ "$CLI_ONLY" -eq 1 ]; then
        echo "NOTICE: VLC/Tkinter missing. Only CLI mode is supported."
        echo "You can install these manually if you want to use the GUI."
        echo "Run via: ./linux-scene-scout-cli.sh"
    fi
    echo "--------------------------------------------------"
else
    echo "Error: Synchronization failed."
    exit 1
fi

# 4. Final Permissions and Cleanup
chmod +x "$SCRIPT_DIR/linux-scene-scout.sh"
chmod +x "$SCRIPT_DIR/linux-scene-scout-cli.sh"

if [ "$CLI_ONLY" -eq 0 ]; then
    echo "Run via: ./linux-scene-scout.sh"
fi