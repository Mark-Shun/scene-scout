#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script's directory
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# Run the application using uv 
"$UV_EXE" run --no-sync src/scenescout.py