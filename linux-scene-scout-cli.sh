#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script's directory
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# Check if installer has been run
if [ ! -f "$SCRIPT_DIR/.install_state" ]; then
    echo "[!] Installation state not found. The application may not be installed."
    read -p "Would you like to run the installer now? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        bash "$SCRIPT_DIR/linux-install.sh"
        exit $?
    else
        exit 1
    fi
fi

# Read custom environment, HuggingFace cache paths, and extras from install state
INSTALL_EXTRA=""
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    CUSTOM_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$CUSTOM_ENV_PATH" ]; then
        export UV_PROJECT_ENVIRONMENT="$CUSTOM_ENV_PATH/.venv"
    fi

    CUSTOM_HF_HOME=$(grep "^HF_HOME=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$CUSTOM_HF_HOME" ]; then
        export HF_HOME="$CUSTOM_HF_HOME"
    fi

    RAW_EXTRA=$(grep "^EXTRA=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$RAW_EXTRA" ]; then
        INSTALL_EXTRA="--extra $RAW_EXTRA"
    fi
fi

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# "$@" passes any arguments given to the shell script directly to the python script
"$UV_EXE" run $INSTALL_EXTRA src/scenescout.py --interactive "$@"

# Pause if there's an error so the terminal doesn't immediately close
if [ $? -ne 0 ]; then
    read -p "Press [Enter] to continue..."
    exit 1
fi