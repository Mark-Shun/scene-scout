#!/bin/bash

# Define the local folder 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

LOGO_ASCII="./assets/logo/logo.txt" 

# --- ASCII Logo Function ---
display_logo() {
    clear
    if [ -f "$LOGO_ASCII" ]; then
        # 'cat' outputs the file content exactly as is
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
    [ -d "/Applications/VLC.app" ] || command -v vlc >/dev/null 2>&1
}

ensure_homebrew() {
    if ! command -v brew >/dev/null 2>&1; then
        echo "Homebrew not found. Attempting to install automatically..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        
        if [ -d "/opt/homebrew/bin" ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -d "/usr/local/bin" ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi
}

fix_homebrew_permissions() {
    # On Intel Macs, Homebrew relies on /usr/local. Check if critical paths are unwritable.
    if [ -d "/usr/local/Cellar" ] && [ ! -w "/usr/local/Cellar" ]; then
        echo "Detected non-writable Homebrew directories in /usr/local."
        echo "Requesting administrator privileges to correct ownership..."
        sudo chown -R $(whoami) /usr/local/Cellar /usr/local/Frameworks /usr/local/Homebrew /usr/local/bin /usr/local/etc /usr/local/include /usr/local/lib /usr/local/opt /usr/local/sbin /usr/local/share /usr/local/var
    fi
}

install_vlc() {
    local force_flag=$1
    echo "Installing VLC..."
    ensure_homebrew
    fix_homebrew_permissions
    
    if [ "$force_flag" = "--force" ]; then
        brew install --cask --force vlc
    else
        brew install --cask vlc
    fi
}

echo "Checking system GUI dependencies..."

if ! check_vlc; then
    echo "VLC was not found. The GUI requires VLC for video playback."
    read -p "Install VLC automatically? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_vlc ""
    fi
else
    echo "VLC is already installed."
    echo "If you are on an Apple Silicon Mac (M1/M2/M3) and experiencing crashes,"
    echo "you may have the older Intel version of VLC installed."
    read -p "Would you like to force-reinstall VLC to ensure compatibility? [y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_vlc "--force"
    fi
fi

if ! check_vlc; then
    echo "[!] Missing critical components. Continuing in CLI-only mode."
    CLI_ONLY=1
fi

# 3. Hardware Selection
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    echo "Intel Mac detected. Automatically selecting compatible CPU fallback..."
    EXTRA="mac-intel"
else
    echo "Apple Silicon Mac detected. Automatically selecting native MPS support..."
    EXTRA="cpu"
fi

# --- Read existing optional paths from previous installs ---
OLD_ENV_PATH=""
OLD_HF_HOME=""
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    OLD_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    OLD_HF_HOME=$(grep "^HF_HOME=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
fi

# --- Initialize Install State (Overwrites old file) ---
echo "EXTRA=$EXTRA" > "$SCRIPT_DIR/.install_state"
[ -n "$FLAGS" ] && echo "FLAGS=$FLAGS" >> "$SCRIPT_DIR/.install_state"
[ -n "$PY_VER" ] && echo "PY_VER=$PY_VER" >> "$SCRIPT_DIR/.install_state"

# --- Custom Environment Path Setup ---
echo "------------------------------------------"
CUSTOM_ENV_PATH=""
if [ -n "$OLD_ENV_PATH" ]; then
    read -p "App files: $OLD_ENV_PATH - (K)eep / (C)hange? [K/c]: " env_choice
    if [ -z "$env_choice" ]; then
        env_choice="K"
    fi
    if [[ "$env_choice" =~ ^[Kk]$ ]]; then
        CUSTOM_ENV_PATH="$OLD_ENV_PATH"
    fi
fi
if [ -z "$CUSTOM_ENV_PATH" ]; then
    read -p "Use custom folder for app files? [y/N]: " use_custom
    if [ -z "$use_custom" ]; then
        use_custom="N"
    fi
    if [[ "$use_custom" =~ ^[Yy]$ ]]; then
        read -p "Enter full absolute path (e.g., /Volumes/Data/scout_env): " CUSTOM_ENV_PATH
        if ! mkdir -p "$CUSTOM_ENV_PATH" 2>/dev/null; then
            echo "[!] Access Denied. Falling back to default."
            CUSTOM_ENV_PATH=""
        fi
    fi
fi
[ -n "$CUSTOM_ENV_PATH" ] && echo "[SUCCESS] App files will be installed to: $CUSTOM_ENV_PATH"

# --- HuggingFace Cache Path Setup ---
CUSTOM_HF_HOME=""
if [ -n "$OLD_HF_HOME" ]; then
    read -p "AI models: $OLD_HF_HOME - (K)eep / (C)hange? [K/c]: " hf_choice
    if [ -z "$hf_choice" ]; then
        hf_choice="K"
    fi
    if [[ "$hf_choice" =~ ^[Kk]$ ]]; then
        CUSTOM_HF_HOME="$OLD_HF_HOME"
    fi
fi
if [ -z "$CUSTOM_HF_HOME" ]; then
    read -p "Use custom folder for AI models? [y/N]: " use_hf
    if [ -z "$use_hf" ]; then
        use_hf="N"
    fi
    if [[ "$use_hf" =~ ^[Yy]$ ]]; then
        read -p "Enter full absolute path (e.g., /Volumes/Data/scout_cache/hf): " CUSTOM_HF_HOME
        if ! mkdir -p "$CUSTOM_HF_HOME" 2>/dev/null; then
            echo "[!] Access Denied. Falling back to default."
            CUSTOM_HF_HOME=""
        fi
    fi
fi
[ -n "$CUSTOM_HF_HOME" ] && echo "[SUCCESS] AI models cache will be set to: $CUSTOM_HF_HOME"
echo "------------------------------------------"

# --- Installation Mode Selection ---
ACTUAL_ENV_PATH="$SCRIPT_DIR/.venv"
if [ -n "$CUSTOM_ENV_PATH" ]; then
    ACTUAL_ENV_PATH="$CUSTOM_ENV_PATH/.venv"
    export UV_PROJECT_ENVIRONMENT="$ACTUAL_ENV_PATH"
fi

if [ -d "$ACTUAL_ENV_PATH" ]; then
    echo "Existing Python environment detected."
    echo "1) Standard Update (Fast - updates modified packages only)"
    echo "2) Clean Install (Fixes corrupted environments and broken dependencies)"
    read -p "Select installation mode [1/2] (Default: 1): " install_mode
    if [ -z "$install_mode" ]; then
        install_mode="1"
    fi
    if [ "$install_mode" = "2" ]; then
        echo "Wiping old environment..."
        rm -rf "$ACTUAL_ENV_PATH"
        echo "Old environment removed. Proceeding with clean install."
    fi
fi

echo "Synchronizing environment with extra: $EXTRA..."

[ -n "$CUSTOM_ENV_PATH" ] && echo "ENV_PATH=$CUSTOM_ENV_PATH" >> "$SCRIPT_DIR/.install_state"
[ -n "$CUSTOM_HF_HOME" ] && echo "HF_HOME=$CUSTOM_HF_HOME" >> "$SCRIPT_DIR/.install_state"

if uv sync --extra "$EXTRA" --python 3.12; then
    echo "--------------------------------------------------"
    echo "Installation successful."
    if [ "$CLI_ONLY" -eq 1 ]; then
        echo "NOTICE: VLC missing. Only CLI mode is supported."
        echo "You can install these manually if you want to use the GUI."
        echo "Run via: ./mac-scene-scout-cli.command"
    fi
    echo "--------------------------------------------------"
else
    echo "Error: Synchronization failed."
    exit 1
fi

# 4. Final Permissions and Cleanup
sed -i '' 's/\r//' "$SCRIPT_DIR/mac-scene-scout.command" 2>/dev/null
chmod +x "$SCRIPT_DIR/mac-scene-scout.command"
xattr -cr "$SCRIPT_DIR" 2>/dev/null

sed -i '' 's/\r//' "$SCRIPT_DIR/mac-scene-scout-cli.command" 2>/dev/null
chmod +x "$SCRIPT_DIR/mac-scene-scout-cli.command"
xattr -cr "$SCRIPT_DIR" 2>/dev/null

if [ "$CLI_ONLY" -eq 0 ]; then
    echo "Run via: ./mac-scene-scout.command"
fi