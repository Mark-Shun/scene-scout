#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 
export UV_PYTHON_INSTALL_DIR="$UV_DIR/python" 
export UV_CACHE_DIR="$UV_DIR/uv_cache"

# Set UV options
export UV_VENV_CLEAR=1

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
        if [ ! -d "/Applications/VLC.app" ]; then
            if ! command -v brew &> /dev/null; then
                read -p "[?] Homebrew not found. Would you like to install it now? [y/n] " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    echo "Installing Homebrew..."
                    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                    
                    # Configure Homebrew for the current shell session (Required for Apple Silicon)
                    if [[ -f /opt/homebrew/bin/brew ]]; then
                        eval "$(/opt/homebrew/bin/brew shellenv)"
                    elif [[ -f /usr/local/bin/brew ]]; then
                        eval "$(/usr/local/bin/brew shellenv)"
                    fi
                else
                    echo "[!] Homebrew is required for automated dependency installation. Skipping..."
                    return 1
                fi
            fi
            echo "Installing VLC and Tcl/Tk via Homebrew..."
            brew install --cask vlc
            brew install tcl-tk
        fi
        echo "VLC is already installed..."
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
read -p "Check and install system dependencies (VLC and tkinter)? [y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    install_dependencies
fi

# Add the isolated folder to this session's PATH 
export PATH="$UV_DIR:$PATH" 

# Interactive Menu
echo "------------------------------------------"
echo "Install options for graphics card acceleration:"
echo "1) NVIDIA CUDA 13.0 (RTX, newer GPUs)"
echo "2) NVIDIA CUDA 12.6 (GTX, older GPUs)"
echo "3) Intel Arc/Xe (XPU)" 
echo "4) AMD ROCm (Linux only)"
echo "5) CPU (Apple MAC: Fast with MPS support (M chips), but slow on regular CPU)"
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
if ! uv venv; then
    echo "Error: Failed to create the virtual environment."
    read -p "Press [Enter] to continue..."
    exit 1
fi

if uv pip install -e .["$EXTRA"]; then
    echo "------------------------------------------"
    echo "Installation successful, you can now open run.sh"
    echo "------------------------------------------"
    read -p "Press [Enter] to continue..."
else
    echo "------------------------------------------"
    echo "Error: Something went wrong during the installation."
    echo "Please check the logs above for details."
    echo "------------------------------------------"
    read -p "Press [Enter] to continue..."
    exit 1
fi