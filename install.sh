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

# Check for VLC Media Player
echo "Checking for VLC..."
if command -v vlc >/dev/null 2>&1; then
    echo "VLC is already installed."
else
    echo "VLC was not found. This application requires VLC for the scene playback viewer."
    read -p "Would you like to attempt to install VLC now? (y/n): " vlc_choice
    
    if [[ "$vlc_choice" =~ ^[Yy]$ ]]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            echo "Detected macOS. Using Homebrew..."
            if command -v brew >/dev/null 2>&1; then
                brew install --cask vlc
            else
                echo "[!] Homebrew not found. Please install VLC manually from https://www.videolan.org/"
                exit 1
            fi
        elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
            echo "Detected Linux. Using apt..."
            sudo apt update && sudo apt install vlc -y
        else
            echo "[!] Unsupported OS for automatic install. Please install VLC manually."
            exit 1
        fi

        # Verify installation success
        if [ $? -ne 0 ]; then
            echo "[!] VLC installation failed."
            exit 1
        fi
    else
        echo "Skipping VLC installation. Please install it manually to ensure the app works."
        exit 1
    fi
fi

# Add the isolated folder to this session's PATH 
export PATH="$UV_DIR:$PATH" 

# Interactive Menu
echo "------------------------------------------"
echo "Install options:"
echo "1) NVIDIA CUDA 13.0 (RTX, newer GPUs)"
echo "2) NVIDIA CUDA 12.6 (GTX, older GPUs)"
echo "3) Intel Arc/Xe (XPU)" [cite: 2]
echo "4) AMD ROCm"
echo "5) CPU (Slow)"
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
echo "Installation succesful, you can now open run.bat"

# Pause functionality for shell
read -p "Press [Enter] to continue..."