#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Check if installer has been run
if [ ! -f "$SCRIPT_DIR/.install_state" ]; then
    echo "[!] Installation state not found. The application may not be installed."
    read -p "Would you like to run the installer now? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        bash "$SCRIPT_DIR/mac-install.sh"
        exit $?
    else
        exit 1
    fi
fi

# 1. Restore environment state paths and extras
INSTALL_EXTRA=""
if [ -f "$SCRIPT_DIR/.install_state" ]; then
    CUSTOM_ENV_PATH=$(grep "^ENV_PATH=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    CUSTOM_HF_HOME=$(grep "^HF_HOME=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    [ -n "$CUSTOM_HF_HOME" ] && export HF_HOME="$CUSTOM_HF_HOME"

    RAW_EXTRA=$(grep "^EXTRA=" "$SCRIPT_DIR/.install_state" | cut -d'=' -f2-)
    if [ -n "$RAW_EXTRA" ]; then
        INSTALL_EXTRA="--extra $RAW_EXTRA"
    fi
fi

# 2. Execute via hardware-specific environment
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    BASE_DIR="${CUSTOM_ENV_PATH:-$SCRIPT_DIR}"
    CONDA_BASE="$BASE_DIR/.conda_base"
    
    # Point directly to the base directory where your packages were installed
    if [ -f "$CONDA_BASE/bin/python" ]; then
        source "$CONDA_BASE/bin/activate"
        python src/scenescout.py
    else
        echo "[!] Environment missing or invalid. Running installer to repair..."
        bash mac-install.sh
    fi
else
    export PYTORCH_ENABLE_MPS_FALLBACK=1
    if [ -n "$CUSTOM_ENV_PATH" ]; then
        export UV_PROJECT_ENVIRONMENT="$CUSTOM_ENV_PATH/.venv"
    fi
    "$SCRIPT_DIR/.uv/uv" run $INSTALL_EXTRA src/scenescout.py
fi