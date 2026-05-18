#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script's directory
cd "$SCRIPT_DIR" || { echo "Failed to enter directory"; exit 1; }

# Define the local folder 
UV_DIR="$SCRIPT_DIR/.uv" 
UV_EXE="$UV_DIR/uv" 

# --- macOS Library Linking ---
if [[ "$OSTYPE" == "darwin"* ]]; then
    # Dynamically find Homebrew base and tcl-tk@8 library path
    if [ -d "/opt/homebrew/opt/tcl-tk@8/lib" ]; then
        BREW_LIB_PATH="/opt/homebrew/opt/tcl-tk@8/lib"
        BREW_BASE="/opt/homebrew"
    elif [ -d "/usr/local/opt/tcl-tk@8/lib" ]; then
        BREW_LIB_PATH="/usr/local/opt/tcl-tk@8/lib"
        BREW_BASE="/usr/local"
    fi
    
    if [ -n "$BREW_LIB_PATH" ]; then
        # Dynamically find the specific version folders (e.g., tcl8.6)
        TCL_DIR=$(ls -d $BREW_LIB_PATH/tcl* | head -n 1)
        TK_DIR=$(ls -d $BREW_LIB_PATH/tk* | head -n 1)
        
        # Link Tkinter to the Homebrew versions
        export TCL_LIBRARY="$TCL_DIR"
        export TK_LIBRARY="$TK_DIR"
        
        # Link VLC and other dynamic libraries
        export DYLD_LIBRARY_PATH="$BREW_BASE/lib:$BREW_LIB_PATH:$DYLD_LIBRARY_PATH"
        
        echo "MacOS environment linked to Homebrew Tcl/Tk 8 libraries."
    fi
fi

# Run the application using uv 
"$UV_EXE" run --no-sync src/scenescout.py