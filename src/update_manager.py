# Scene Scout - Natural language video scene search
# Copyright (C) 2026 Mark-Shun/Sonicfreak1111
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import zipfile
import tempfile
import requests
import subprocess
import shutil
from pathlib import Path


def download_and_extract_update(zip_url: str, progress_callback=None) -> str:
    """Downloads the release ZIP, extracts it, and returns the path to the inner source folder."""
    temp_dir = tempfile.mkdtemp(prefix="scenescout_update_")
    zip_path = os.path.join(temp_dir, "update.zip")

    try:
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

    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise e


def generate_updater_script(extracted_folder: str, target_dir: str, app_mode: str = 'gui') -> str:
    """Generates the OS-specific update script, dynamically targeting the correct environment launcher."""

    # Store the root temp directory so the script can completely wipe it
    temp_root = os.path.dirname(extracted_folder)

    # --- Fail-Safe: Read and Validate State ---
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

    if sys.platform == 'win32':
        # Write to target_dir with a unique hidden name to prevent xcopy byte-offset corruption
        script_path = os.path.join(target_dir, ".scout_updater.bat")
        uv_exe = os.path.join(target_dir, ".uv", "uv.exe")
        uv_cmd = f'"{uv_exe}"' if os.path.exists(uv_exe) else "uv"

        # Determine Windows Post-Launch Target
        if app_mode == 'gui':
            launch_cmd = f'start "" "{os.path.join(target_dir, "windows-scene-scout.bat")}"'
        elif app_mode == 'cli':
            launch_cmd = f'start "" "{os.path.join(target_dir, "windows-scene-scout-cli.bat")}"'
        else:
            launch_cmd = ":: Silent update complete. No relaunch requested."

        if has_valid_state:
            post_copy_action = f"""
echo Synchronizing dependencies...
cd /d "{target_dir}"
{uv_cmd} sync {uv_cmd_args}
if errorlevel 1 goto ROLLBACK

:: Success: Remove backup and launch
if exist "{target_dir}\\src_backup" rmdir /S /Q "{target_dir}\\src_backup"
{launch_cmd}
goto END
"""
        else:
            post_copy_action = f"""
echo Original installation state not found.
echo Launching manual installer to re-configure hardware...
cd /d "{target_dir}"
start "" "{os.path.join(target_dir, 'windows-install.bat')}"
goto END
"""

        script_content = f"""@echo off
echo Applying Scene Scout Update...
ping 127.0.0.1 -n 4 > nul

:: Backup current source
if exist "{target_dir}\\src" move "{target_dir}\\src" "{target_dir}\\src_backup"

:: Copy new files
xcopy /Y /E /H /C /I "{extracted_folder}\\*" "{target_dir}"
if errorlevel 1 goto ROLLBACK

{post_copy_action}

:ROLLBACK
echo [ERROR] Update failed. Rolling back to previous version...
if exist "{target_dir}\\src" rmdir /S /Q "{target_dir}\\src"
if exist "{target_dir}\\src_backup" move "{target_dir}\\src_backup" "{target_dir}\\src"
rmdir /S /Q "{temp_root}"
del "%~f0"
exit /b 1

:END
rmdir /S /Q "{temp_root}"
del "%~f0"
exit
"""
    else:
        script_path = os.path.join(target_dir, ".scout_updater.sh")
        uv_exe = os.path.join(target_dir, ".uv", "uv")
        install_script = "mac-install.sh" if sys.platform == 'darwin' else "linux-install.sh"

        # Determine Unix Post-Launch Target
        if app_mode == 'gui':
            target_sh = "mac-scene-scout.command" if sys.platform == 'darwin' else "linux-scene-scout.sh"
            launch_cmd = f'nohup "{os.path.join(target_dir, target_sh)}" > /dev/null 2>&1 &'
        elif app_mode == 'cli':
            target_sh = "mac-scene-scout-cli.command" if sys.platform == 'darwin' else "linux-scene-scout-cli.sh"
            launch_cmd = f'gnome-terminal -- "{os.path.join(target_dir, target_sh)}" || xterm -e "{os.path.join(target_dir, target_sh)}" || bash "{os.path.join(target_dir, target_sh)}"'
        else:
            launch_cmd = "# Silent update complete."

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
    rm -rf "{temp_root}"
    rm -- "$0"
    exit 1
fi

# Success: Remove backup and launch
rm -rf "{target_dir}/src_backup"
chmod +x "{os.path.join(target_dir, target_sh)}" 2>/dev/null
{launch_cmd}
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
    rm -rf "{temp_root}"
    rm -- "$0"
    exit 1
fi

{post_copy_action}

rm -rf "{temp_root}"
rm -- "$0"
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


def trigger_update_handoff(download_url: str, is_source_zip: bool = True, progress_callback=None, app_mode: str = 'gui'):
    """Executes the complete update handoff sequence, passing the app_mode downstream."""
    import config

    target_dir = str(config.PROJECT_ROOT)
    log_path = config.PROJECT_ROOT / "update_handoff.log"

    if is_source_zip:
        extracted_folder = download_and_extract_update(download_url, progress_callback)
        script_path = generate_updater_script(extracted_folder, target_dir, app_mode)
    else:
        raise NotImplementedError("Binary update path is not yet implemented.")

    if sys.platform == 'win32':
        cmd_str = f'cmd.exe /c ""{script_path}" > "{log_path}" 2>&1"'
        subprocess.Popen(cmd_str, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        with open(log_path, "w", encoding="utf-8") as log_file:
            subprocess.Popen(
                [script_path],
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
