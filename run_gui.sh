#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# Add the local uv directory to the PATH for this session
export PATH="$UV_DIR:$PATH"

# --- macOS Library Linking ---
if [[ "$OSTYPE" == "darwin"* ]]; then
    # Standard path for Homebrew on Apple Silicon
    BREW_LIB_PATH="/opt/homebrew/opt/tcl-tk/lib"
    
    if [ -d "$BREW_LIB_PATH" ]; then
        # Dynamically find the specific version folders (e.g., tcl8.6)
        TCL_DIR=$(ls -d $BREW_LIB_PATH/tcl* | head -n 1)
        TK_DIR=$(ls -d $BREW_LIB_PATH/tk* | head -n 1)
        
        # Link Tkinter to the Homebrew versions
        export TCL_LIBRARY="$TCL_DIR"
        export TK_LIBRARY="$TK_DIR"
        
        # Link VLC and other dynamic libraries
        export DYLD_LIBRARY_PATH="/opt/homebrew/lib:/opt/homebrew/opt/tcl-tk/lib:$DYLD_LIBRARY_PATH"
        
        echo "MacOS environment linked to Homebrew libraries."
    fi
fi


# Run the application using uv 
uv run --no-sync src/scenescout.py