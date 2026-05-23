#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script's directory
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    export PYTORCH_ENABLE_MPS_FALLBACK=1
fi

# Read custom environment path if it exists
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    CUSTOM_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$CUSTOM_ENV_PATH" ]; then
        export UV_PROJECT_ENVIRONMENT="$CUSTOM_ENV_PATH/.venv"
    fi
fi

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# Run the application using uv 
"$UV_EXE" run --no-sync src/scenescout.py