import os
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, Signal
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


class UpdateDialog(QDialog):
    """A modal dialog displaying release notes for a new version."""

    progress_updated = Signal(int)

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

        header = QLabel(f"Version {update_info['latest_version']} is available!")
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

        self.install_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.progress_bar.show()

        self.download_btn.setText("Downloading...")

        version = self.update_info['latest_version']
        zip_url = f"https://github.com/Mark-Shun/scene-scout/archive/refs/tags/v{version}.zip"

        import threading

        def run_update():
            try:
                from update_manager import download_and_extract_update, generate_updater_script
                import config as cfg

                target_dir = str(cfg.PROJECT_ROOT)
                extracted_folder = download_and_extract_update(
                    zip_url,
                    progress_callback=lambda p: self.progress_updated.emit(p),
                )
                self._do_handoff(extracted_folder, target_dir)
            except Exception as e:
                self._update_failed(str(e))

        threading.Thread(target=run_update, daemon=True).start()

    def _do_handoff(self, extracted_folder: str, target_dir: str):
        from update_manager import generate_updater_script
        import subprocess

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

        QApplication.quit()
        import os
        os._exit(0)

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
