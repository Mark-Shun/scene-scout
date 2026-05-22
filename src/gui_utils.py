import os
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextBrowser, QWidget,
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

    def __init__(self, parent, update_info: dict):
        super().__init__(parent)
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

        btn_layout = QHBoxLayout()
        download_btn = QPushButton("Go to Release Page")
        download_btn.clicked.connect(
            lambda: webbrowser.open_new(update_info['url'])
        )
        btn_layout.addWidget(download_btn)

        btn_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        center_window(self)


def show_update_dialog(parent, update_info: dict) -> None:
    """Convenience wrapper that creates and shows the UpdateDialog."""
    dialog = UpdateDialog(parent, update_info)
    dialog.exec()
