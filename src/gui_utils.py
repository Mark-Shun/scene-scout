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
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QThread, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QProgressBar, QMessageBox, QTextBrowser, QWidget,
)


def load_app_icon(icon_path: str) -> QIcon:
    """Loads and returns a QIcon from the given path."""
    try:
        return QIcon(str(icon_path))
    except Exception as e:
        print(f"Icon Load Warning: {e}")
        return QIcon()


def center_window(window: QWidget) -> None:
    """Centers a QWidget/QMainWindow/QDialog on the primary screen."""
    screen = QApplication.primaryScreen().geometry()
    size = window.geometry()
    x = (screen.width() - size.width()) // 2
    y = (screen.height() - size.height()) // 2
    window.move(x, y)


class UpdateWorker(QThread):
    """Native Qt worker to handle network and extraction without crashing the GUI loop."""
    progress = Signal(int)
    finished_script = Signal(str)
    error = Signal(str)

    def __init__(self, download_url: str, is_source_zip: bool):
        super().__init__()
        self.download_url = download_url
        self.is_source_zip = is_source_zip

    def run(self):
        import config
        from update_manager import verify_environment, download_and_extract_update, generate_updater_script
        try:
            self.progress.emit(5)
            target_dir = str(config.PROJECT_ROOT)

            if not verify_environment(target_dir):
                raise RuntimeError("Dependency pre-check failed. Network might be unstable.")

            if self.is_source_zip:
                extracted_folder = download_and_extract_update(
                    self.download_url,
                    progress_callback=lambda p: self.progress.emit(10 + int(p * 0.85))
                )
                self.progress.emit(95)
                script_path = generate_updater_script(extracted_folder, target_dir, app_mode='gui')
                self.progress.emit(100)
                self.finished_script.emit(script_path)
            else:
                raise NotImplementedError("Binary update is not yet implemented.")
        except Exception as e:
            self.error.emit(str(e))


class UpdateDialog(QDialog):
    """A modal dialog displaying release notes for a new version."""

    progress_updated = Signal(int)
    update_ready = Signal()

    def __init__(self, parent, update_info: dict):
        super().__init__(parent)
        self.update_info = update_info
        self.setWindowTitle("Update Available")
        self.setMinimumSize(600, 700)
        self.resize(600, 700)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        header = QLabel(f"Version {update_info['latest_version']} is available! (Current: v{update_info['current_version']})")
        header.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(header)

        image_bytes = update_info.get("image_bytes")
        if image_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(image_bytes)
            img_label = QLabel()
            scaled_pixmap = pixmap.scaled(
                560, 200,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            img_label.setPixmap(scaled_pixmap)
            img_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(img_label)

        text_browser = QTextBrowser()
        text_browser.setOpenExternalLinks(True)
        text_browser.setMarkdown(update_info.get('notes', ''))
        layout.addWidget(text_browser)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.progress_updated.connect(self.progress_bar.setValue)
        self.update_ready.connect(self.execute_shutdown)

        btn_layout = QHBoxLayout()

        self.install_btn = QPushButton("Install Update Automatically")
        self.install_btn.setStyleSheet("font-weight: bold; background-color: #0078D7; color: white;")
        self.install_btn.clicked.connect(self.start_automated_update)
        btn_layout.addWidget(self.install_btn)

        self.download_btn = QPushButton("Manual Download")
        self.download_btn.clicked.connect(
            lambda: webbrowser.open_new(update_info['url'])
        )
        btn_layout.addWidget(self.download_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

        center_window(self)

    def start_automated_update(self):
        reply = QMessageBox.question(
            self, 'Confirm Update',
            'Scene Scout will download the update, restart, and apply the new files. Continue?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        download_url = self.update_info.get('download_url')
        is_source_zip = self.update_info.get('is_source_zip', True)

        if not download_url:
            QMessageBox.critical(self, "Update Error", "Could not find a valid download link from the API.")
            return

        self.install_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress_bar.show()

        self.download_btn.setText("Downloading...")

        self.worker = UpdateWorker(download_url, is_source_zip)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished_script.connect(self._on_update_prepared)
        self.worker.error.connect(self._update_failed)
        self.worker.start()

    @Slot(str)
    def _on_update_prepared(self, script_path: str):
        self.worker.wait()

        import subprocess, config
        log_path = config.PROJECT_ROOT / "update_handoff.log"
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

        self.execute_shutdown()

    @Slot()
    def execute_shutdown(self):
        """Allows Qt to cleanly unwind the signal stack before terminating."""
        QApplication.closeAllWindows()
        QApplication.quit()
        QTimer.singleShot(500, lambda: os._exit(0))

    def _update_failed(self, msg: str):
        self.progress_bar.hide()
        self.download_btn.setText("Manual Download")
        self.install_btn.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        QMessageBox.critical(self, 'Update Failed', f'Update failed: {msg}')


def show_update_dialog(parent, update_info: dict) -> None:
    """Convenience wrapper that creates and shows the UpdateDialog."""
    dialog = UpdateDialog(parent, update_info)
    dialog.exec()
