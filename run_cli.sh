#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# Add the local uv directory to the PATH for this session
export PATH="$UV_DIR:$PATH"

# "$@" passes any arguments given to the shell script directly to the python script
uv run --no-sync src/scenescout.py --interactive "$@"

# Pause if there's an error so the terminal doesn't immediately close
if [ $? -ne 0 ]; then
    read -p "Press [Enter] to continue..."
    exit 1
fi