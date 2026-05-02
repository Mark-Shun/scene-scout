#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# Add the local uv directory to the PATH for this session
export PATH="$UV_DIR:$PATH"

# Run the application using uv 
uv run --no-sync src/scenescout.py