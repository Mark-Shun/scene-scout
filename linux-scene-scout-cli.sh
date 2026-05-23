#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script's directory
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# Read custom environment and HuggingFace cache paths if they exist
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    CUSTOM_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$CUSTOM_ENV_PATH" ]; then
        export UV_PROJECT_ENVIRONMENT="$CUSTOM_ENV_PATH/.venv"
    fi

    CUSTOM_HF_HOME=$(grep "^HF_HOME=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$CUSTOM_HF_HOME" ]; then
        export HF_HOME="$CUSTOM_HF_HOME"
    fi
fi

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# "$@" passes any arguments given to the shell script directly to the python script
"$UV_EXE" run --no-sync src/scenescout.py --interactive "$@"

# Pause if there's an error so the terminal doesn't immediately close
if [ $? -ne 0 ]; then
    read -p "Press [Enter] to continue..."
    exit 1
fi