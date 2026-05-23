import os
import sys
import zipfile
import tempfile
import requests
import subprocess
from pathlib import Path


def download_and_extract_update(zip_url: str, progress_callback=None) -> str:
    """Downloads the release ZIP, extracts it, and returns the path to the inner source folder."""
    temp_dir = tempfile.mkdtemp(prefix="scenescout_update_")
    zip_path = os.path.join(temp_dir, "update.zip")

    with requests.get(zip_url, stream=True, timeout=10) as r:
        r.raise_for_status()
        total_length = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(zip_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total_length > 0:
                    progress_callback(int(100 * downloaded / total_length))

    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(temp_dir)

    os.remove(zip_path)

    extracted_items = os.listdir(temp_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(temp_dir, extracted_items[0])):
        return os.path.join(temp_dir, extracted_items[0])

    return temp_dir


def generate_updater_script(extracted_folder: str, target_dir: str) -> str:
    """Generates the OS-specific update script and returns its path."""
    script_path = os.path.join(extracted_folder, "apply_update")

    # --- Fail-Safe 1 & 3: Read and Validate State ---
    state_file = os.path.join(target_dir, ".install_state")
    extra_arg, flags_arg, py_ver_arg = "", "", ""
    has_valid_state = False

    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    if key == "EXTRA":
                        extra_arg = f"--extra {val}"
                    elif key == "FLAGS":
                        flags_arg = val
                    elif key == "PY_VER":
                        py_ver_arg = val

        if extra_arg:
            has_valid_state = True

    uv_cmd_args = f"{extra_arg} {flags_arg} {py_ver_arg}".strip()
    # ------------------------------------------------

    if sys.platform == 'win32':
        script_path += ".bat"
        uv_exe = os.path.join(target_dir, ".uv", "uv.exe")

        if has_valid_state:
            post_copy_action = f"""
echo Synchronizing dependencies...
cd /d "{target_dir}"
"{uv_exe}" sync {uv_cmd_args}
if errorlevel 1 goto ROLLBACK

:: Success: Remove backup and launch
if exist "{target_dir}\\src_backup" rmdir /S /Q "{target_dir}\\src_backup"
start "" "{os.path.join(target_dir, 'windows-scene-scout.bat')}"
goto END

:ROLLBACK
echo [ERROR] Update failed. Rolling back to previous version...
if exist "{target_dir}\\src" rmdir /S /Q "{target_dir}\\src"
if exist "{target_dir}\\src_backup" move "{target_dir}\\src_backup" "{target_dir}\\src"
pause
exit /b 1

:END
"""
        else:
            post_copy_action = f"""
echo Original installation state not found.
echo Launching manual installer to re-configure hardware...
cd /d "{target_dir}"
start "" "{os.path.join(target_dir, 'windows-install.bat')}"
"""

        script_content = f"""@echo off
echo Applying Scene Scout Update...
timeout /t 3 /nobreak >nul

:: Backup current source
if exist "{target_dir}\\src" move "{target_dir}\\src" "{target_dir}\\src_backup"

:: Copy new files
xcopy /Y /E /H /C /I "{extracted_folder}\\*" "{target_dir}\\"
if errorlevel 1 goto ROLLBACK

{post_copy_action}

rmdir /S /Q "{os.path.dirname(extracted_folder)}"
exit

:ROLLBACK
echo [ERROR] Update failed. Rolling back to previous version...
if exist "{target_dir}\\src" rmdir /S /Q "{target_dir}\\src"
if exist "{target_dir}\\src_backup" move "{target_dir}\\src_backup" "{target_dir}\\src"
pause
exit /b 1
"""
    else:
        script_path += ".sh"
        uv_exe = os.path.join(target_dir, ".uv", "uv")
        install_script = "mac-install.sh" if sys.platform == 'darwin' else "linux-install.sh"
        launch_script = "mac-scene-scout.command" if sys.platform == 'darwin' else "linux-scene-scout.sh"

        if has_valid_state:
            post_copy_action = f"""
echo "Synchronizing dependencies..."
cd "{target_dir}"
export UV_PYTHON_INSTALL_DIR="{os.path.join(target_dir, '.uv', 'python')}"
export UV_CACHE_DIR="{os.path.join(target_dir, '.uv', 'uv_cache')}"

if ! "{uv_exe}" sync {uv_cmd_args}; then
    echo "[ERROR] Dependency sync failed! Initiating rollback..."
    rm -rf "{target_dir}/src"
    [ -d "{target_dir}/src_backup" ] && mv "{target_dir}/src_backup" "{target_dir}/src"
    read -p "Press any key to exit..." -n1 -s
    exit 1
fi

# Success: Remove backup and launch
rm -rf "{target_dir}/src_backup"
chmod +x "{os.path.join(target_dir, launch_script)}"
nohup "{os.path.join(target_dir, launch_script)}" > /dev/null 2>&1 &
"""
        else:
            post_copy_action = f"""
echo "Original installation state not found."
echo "Launching manual installer to re-configure hardware..."
cd "{target_dir}"
chmod +x "{os.path.join(target_dir, install_script)}"
gnome-terminal -- "./{install_script}" || xterm -e "./{install_script}" || bash "./{install_script}"
"""

        script_content = f"""#!/bin/bash
echo "Applying Scene Scout Update..."
sleep 3

# Backup current source
[ -d "{target_dir}/src" ] && mv "{target_dir}/src" "{target_dir}/src_backup"

if ! cp -a "{extracted_folder}/." "{target_dir}/"; then
    echo "[ERROR] File copy failed. Initiating rollback..."
    rm -rf "{target_dir}/src"
    [ -d "{target_dir}/src_backup" ] && mv "{target_dir}/src_backup" "{target_dir}/src"
    read -p "Press any key to exit..." -n1 -s
    exit 1
fi

{post_copy_action}

rm -rf "{os.path.dirname(extracted_folder)}"
exit 0
"""

    with open(script_path, "w", encoding="utf-8", newline='\n') as f:
        f.write(script_content)

    if sys.platform != 'win32':
        os.chmod(script_path, 0o755)

    return script_path


def verify_environment(target_dir: str) -> bool:
    """Runs a dry-run sync to ensure dependencies can resolve before closing the app."""
    state_file = os.path.join(target_dir, ".install_state")
    if not os.path.exists(state_file):
        return True

    extra_arg, flags_arg, py_ver_arg = "", "", ""
    with open(state_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                key, val = line.strip().split("=", 1)
                if key == "EXTRA":
                    extra_arg = f"--extra {val}"
                elif key == "FLAGS":
                    flags_arg = val
                elif key == "PY_VER":
                    py_ver_arg = val

    uv_exe = os.path.join(target_dir, ".uv", "uv.exe" if sys.platform == 'win32' else "uv")
    if not os.path.exists(uv_exe):
        return True

    cmd = [uv_exe, "sync", "--dry-run"] + f"{extra_arg} {flags_arg} {py_ver_arg}".split()

    try:
        result = subprocess.run(
            cmd, cwd=target_dir, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        return result.returncode == 0
    except Exception:
        return False


def trigger_update_handoff(download_url: str, is_source_zip: bool = True, progress_callback=None):
    """Executes the complete update handoff sequence."""
    import config

    target_dir = str(config.PROJECT_ROOT)

    if is_source_zip:
        extracted_folder = download_and_extract_update(download_url, progress_callback)
        script_path = generate_updater_script(extracted_folder, target_dir)
    else:
        raise NotImplementedError("Binary update path is not yet implemented.")

    if sys.platform == 'win32':
        subprocess.Popen(
            [script_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.Popen(
            [script_path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
