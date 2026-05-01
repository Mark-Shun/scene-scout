#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 
export UV_PYTHON_INSTALL_DIR="$UV_DIR/python" 
export UV_CACHE_DIR="$UV_DIR/uv_cache" 

# Install uv locally if missing 
if [ ! -f "$UV_EXE" ]; then
    echo "Downloading uv to isolated folder..." 
    mkdir -p "$UV_DIR" 
    # Use the official installation script for Unix systems
    export UV_INSTALL_DIR="$UV_DIR"
    export UV_UNMANAGED_INSTALL="1"
    curl -LsSf https://astral.sh/uv/install.sh | sh 
fi

# --- System Dependency Check ---
install_dependencies() {
    # Detect Operating System
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "Detected macOS..."
        if ! command -v brew &> /dev/null; then
            echo "[!] Homebrew not found. Please install it at https://brew.sh/ to automate this process."
            return 1
        fi
        echo "Installing VLC and Tcl/Tk via Homebrew..."
        brew install --cask vlc
        brew install tcl-tk
    elif [ -f /etc/os-release ]; then
        . /etc/os-release
        case "$ID" in
            ubuntu|debian|linuxmint|lubuntu)
                echo "Detected Debian-based system ($ID)..."
                sudo apt update && sudo apt install -y python3-tk vlc
                ;;
            arch|manjaro)
                echo "Detected Arch-based system ($ID)..."
                sudo pacman -S --needed --noconfirm tk vlc vlc-plugin-ffmpeg vlc-plugin-mpeg2 vlc-plugin-x246 vlc-plugin-x265 vlc-plugin-matroska
                ;;
            *)
                echo "Unknown Linux distribution. Please install 'tk' and 'vlc' manually."
                ;;
        esac
    fi
}

# Ask the user for dependency check
read -p "Check and install system dependencies (VLC & Tkinter)? [y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    install_dependencies
fi

# Add the isolated folder to this session's PATH 
export PATH="$UV_DIR:$PATH" 

# Interactive Menu
echo "------------------------------------------"
echo "Install options:"
echo "1) NVIDIA CUDA 13.0 (RTX, newer GPUs)"
echo "2) NVIDIA CUDA 12.6 (GTX, older GPUs)"
echo "3) Intel Arc/Xe (XPU)" 
echo "4) AMD ROCm"
echo "5) CPU (Apple MAC: Fast with MPS support, but slow on regular CPU)"
echo "------------------------------------------"

read -p "Select an option [1-5]: " user_choice

# Map choices to uv extras
case "$user_choice" in
    1) EXTRA="cu130" ;;
    2) EXTRA="cu126" ;;
    3) EXTRA="xpu" ;;
    4) EXTRA="rocm" ;;
    5) EXTRA="cpu" ;;
    *) 
        echo "Error: Invalid selection."
        exit 1 
        ;;
esac

echo "Running installer with extra: $EXTRA..."
uv sync --extra "$EXTRA"
echo "Installation succesful, you can now open run.sh"

# Pause functionality for shell
read -p "Press [Enter] to continue..."