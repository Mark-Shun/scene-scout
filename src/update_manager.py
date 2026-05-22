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

    if sys.platform == 'win32':
        script_path += ".bat"
        uv_exe = os.path.join(target_dir, ".uv", "uv.exe")
        launcher = os.path.join(target_dir, "windows-scene-scout.bat")

        script_content = f"""@echo off
echo Applying Scene Scout Update...
timeout /t 3 /nobreak >nul

xcopy /Y /E /H /C /I "{extracted_folder}\\*" "{target_dir}\\"

echo Synchronizing dependencies...
cd /d "{target_dir}"
"{uv_exe}" sync

start "" "{launcher}"

rmdir /S /Q "{os.path.dirname(extracted_folder)}"
exit
"""
    else:
        script_path += ".sh"
        uv_exe = os.path.join(target_dir, ".uv", "uv")
        if sys.platform == 'darwin':
            launcher = os.path.join(target_dir, "mac-scene-scout.command")
        else:
            launcher = os.path.join(target_dir, "linux-scene-scout.sh")

        script_content = f"""#!/bin/bash
echo "Applying Scene Scout Update..."
sleep 3

cp -a "{extracted_folder}/." "{target_dir}/"

echo "Synchronizing dependencies..."
cd "{target_dir}"
export UV_PYTHON_INSTALL_DIR="{os.path.join(target_dir, '.uv', 'python')}"
export UV_CACHE_DIR="{os.path.join(target_dir, '.uv', 'uv_cache')}"
"{uv_exe}" sync

chmod +x "{launcher}"
nohup "{launcher}" > /dev/null 2>&1 &

rm -rf "{os.path.dirname(extracted_folder)}"
exit 0
"""

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    if sys.platform != 'win32':
        os.chmod(script_path, 0o755)

    return script_path


def trigger_update_handoff(zip_url: str, progress_callback=None):
    """Executes the complete update handoff sequence."""
    import config

    target_dir = str(config.PROJECT_ROOT)
    extracted_folder = download_and_extract_update(zip_url, progress_callback)
    script_path = generate_updater_script(extracted_folder, target_dir)

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

    from PySide6.QtWidgets import QApplication

    QApplication.quit()
    sys.exit(0)
