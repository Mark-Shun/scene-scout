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
        self.setMinimumSize(600, 500)
        self.resize(600, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel("A new version of Scene Scout is available!")
        header.setStyleSheet("font-size: 12px; font-weight: bold;")
        layout.addWidget(header)

        version_label = QLabel(
            f"Current: v{update_info['current_version']}  \u2794  Latest: v{update_info['latest_version']}"
        )
        layout.addWidget(version_label)
        layout.addSpacing(15)

        notes_label = QLabel("Release Notes")
        notes_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(notes_label)

        text_browser = QTextBrowser()
        text_browser.setOpenExternalLinks(True)
        text_browser.setMarkdown(update_info.get('notes', ''))
        layout.addWidget(text_browser)

        btn_layout = QHBoxLayout()
        download_btn = QPushButton("Open Release Page")
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
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)


def show_update_dialog(parent, update_info: dict) -> None:
    """Convenience wrapper that creates and shows the UpdateDialog."""
    dialog = UpdateDialog(parent, update_info)
    dialog.exec()
