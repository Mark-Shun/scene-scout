#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

LOGO_ASCII="./assets/logo/logo.txt" 

# --- ASCII Logo Function ---
display_logo() {
    clear
    if [ -f "$LOGO_ASCII" ]; then
        cat "$LOGO_ASCII"
    fi
    echo ""
}

display_logo
# --------------------------------

echo "---Mac installation script for Scene Scout---"

# --- START UPDATE CHECK ---
echo "Checking for updates..."
REMOTE_TAG=$(curl -s --connect-timeout 2 https://api.github.com/repos/Mark-Shun/scene-scout/releases/latest | grep '"tag_name":' | sed -E 's/.*"v?([^"]+)".*/\1/')
LOCAL_VER=$(grep '^version =' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')

if [ -n "$REMOTE_TAG" ] && [ -n "$LOCAL_VER" ]; then
    LOWER_VER=$(printf '%s\n%s' "$LOCAL_VER" "$REMOTE_TAG" | sort -V | head -n 1)
    if [ "$LOCAL_VER" != "$REMOTE_TAG" ] && [ "$LOWER_VER" = "$LOCAL_VER" ]; then
        echo -e "\n\033[1;36m[UPDATE] A newer version (v$REMOTE_TAG) is available!\033[0m"
    fi
fi

# 1. Dependency Check Logic
CLI_ONLY=0
check_vlc() { [ -d "/Applications/VLC.app" ] || command -v vlc >/dev/null 2>&1; }
install_vlc() {
    if ! command -v brew >/dev/null 2>&1; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    brew install --cask vlc
}

if ! check_vlc; then
    read -p "Install VLC automatically? [y/n] " -n 1 -r; echo
    [[ $REPLY =~ ^[Yy]$ ]] && install_vlc
fi
! check_vlc && CLI_ONLY=1

# 2. State & Path Management
OLD_ENV_PATH=""
OLD_HF_HOME=""
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    OLD_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    OLD_HF_HOME=$(grep "^HF_HOME=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
fi

CUSTOM_ENV_PATH=""
if [ -n "$OLD_ENV_PATH" ]; then
    read -p "App files: $OLD_ENV_PATH - (K)eep / (C)hange? [K/c]: " choice
    [[ "$choice" =~ ^[Kk]$ ]] && CUSTOM_ENV_PATH="$OLD_ENV_PATH"
fi
if [ -z "$CUSTOM_ENV_PATH" ]; then
    read -p "Use custom folder for app files? [y/N]: " use_custom
    [[ "$use_custom" =~ ^[Yy]$ ]] && read -p "Enter absolute path: " CUSTOM_ENV_PATH
fi

CUSTOM_HF_HOME=""
if [ -n "$OLD_HF_HOME" ]; then
    read -p "AI models: $OLD_HF_HOME - (K)eep / (C)hange? [K/c]: " choice
    [[ "$choice" =~ ^[Kk]$ ]] && CUSTOM_HF_HOME="$OLD_HF_HOME"
fi
if [ -z "$CUSTOM_HF_HOME" ]; then
    read -p "Use custom folder for AI models? [y/N]: " use_hf
    [[ "$use_hf" =~ ^[Yy]$ ]] && read -p "Enter absolute path: " CUSTOM_HF_HOME
fi

# 3. Hardware-Aware Environment Setup
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    echo "Intel Mac detected. Setting up Conda..."
    
    # Use custom path if provided, otherwise default to script directory
    BASE_DIR="${CUSTOM_ENV_PATH:-$SCRIPT_DIR}"
    CONDA_BASE="$BASE_DIR/.conda_base"

    # Check for spaces (Miniconda installer restriction)
    if [[ "$CONDA_BASE" == *" "* ]]; then
        echo -e "\n\033[1;31m[ERROR] Installation path contains spaces.\033[0m"
        echo "Path: '$CONDA_BASE'"
        echo "The Conda installer cannot be placed in a folder with spaces."
        echo "Please do ONE of the following:"
        echo "  1. Rename your folder (e.g., change 'scene scout' to 'scene-scout')"
        echo "  2. Run the script again."
        exit 1
    fi

    # Install Miniconda if missing
    if [ ! -f "$CONDA_BASE/bin/conda" ]; then
        curl -L -o miniconda.sh "https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh"
        bash miniconda.sh -b -p "$CONDA_BASE"
        rm miniconda.sh
    fi
    
    # Activate the base environment
    source "$CONDA_BASE/bin/activate"
    
    echo "Installing PyTorch via conda-forge..."
    # --override-channels prevents Conda from checking the default 'main' channel, bypassing the ToS prompt completely
    if ! conda install -y -c conda-forge --override-channels python=3.13 pytorch torchvision; then
        echo -e "\n\033[1;31m[ERROR] Conda package installation failed.\033[0m"
        echo "The installation has been aborted. Please check the error messages above."
        exit 1
    fi
    
    echo "Installing remaining dependencies via pip..."
    if ! pip install av imageio-ffmpeg numpy opencv-python-headless pillow python-vlc requests scenedetect toml tqdm transformers hf-transfer pyside6-essentials qt-material psutil; then
        echo -e "\n\033[1;31m[ERROR] Pip package installation failed.\033[0m"
        echo "The installation has been aborted. Please check the error messages above."
        exit 1
    fi
    
    EXTRA="cpu"
    
else
    echo "Apple Silicon detected. Setting up uv..."
    EXTRA="cpu"
    UV_DIR="$SCRIPT_DIR/.uv"
    
    export UV_PYTHON_INSTALL_DIR="$UV_DIR/python" 
    export UV_CACHE_DIR="$UV_DIR/uv_cache"
    export UV_VENV_CLEAR=1
    export UV_INSTALL_DIR="$UV_DIR"
    export UV_UNMANAGED_INSTALL="1"
    
    [ ! -f "$UV_DIR/uv" ] && curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$UV_DIR:$PATH"
fi

# 4. Save State
echo "EXTRA=$EXTRA" > "$SCRIPT_DIR/.install_state"
[ -n "$CUSTOM_ENV_PATH" ] && echo "ENV_PATH=$CUSTOM_ENV_PATH" >> "$SCRIPT_DIR/.install_state"
[ -n "$CUSTOM_HF_HOME" ] && echo "HF_HOME=$CUSTOM_HF_HOME" >> "$SCRIPT_DIR/.install_state"

# 5. Sync (Only for Apple Silicon)
if [ "$ARCH" != "x86_64" ]; then
    export UV_PROJECT_ENVIRONMENT="${CUSTOM_ENV_PATH:-$SCRIPT_DIR}/.venv"
    if ! uv sync --extra "$EXTRA" --python 3.12; then
        echo "Error: Synchronization failed."
        exit 1
    fi
fi

# 6. Final Permissions and Cleanup
chmod +x "$SCRIPT_DIR/mac-scene-scout.command"
xattr -cr "$SCRIPT_DIR" 2>/dev/null

echo "--------------------------------------------------"
echo "Installation complete."
if [ "$CLI_ONLY" -eq 1 ]; then
    echo "NOTICE: VLC missing. Only CLI mode is supported."
    echo "You can install VLC manually if you want to use the GUI."
fi
echo "Run via: ./mac-scene-scout.command"
echo "--------------------------------------------------"