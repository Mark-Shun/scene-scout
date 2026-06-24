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

import sys
import multiprocessing

from PySide6.QtGui import QPixmap, QPalette
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qt_material import apply_stylesheet

import config
import utils

if __name__ == '__main__':
    utils.setup_logging()
    utils.check_environment_packages()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)

    saved_config = config.load_config()
    default_theme = config.DEFAULT_CONFIG.get('theme', 'dark_lightgreen.xml')
    initial_theme = saved_config.get('theme', default_theme)

    def apply_initial_theme(theme_name):
        is_dark = 'dark' in theme_name.lower()

        if theme_name.endswith('.xml'):
            apply_stylesheet(app, theme=theme_name, extra={'density_scale': '0'})
            overlay_path = config.THEMES_DIR / 'qt_material_overlay.css'
            if overlay_path.exists():
                css = overlay_path.read_text(encoding='utf-8')
                css = (css
                    .replace('ACCENT_COLOR', '#FFCA28' if is_dark else '#FFA000')
                    .replace('ACCENT_HOVER', 'rgba(255, 202, 40, 0.15)' if is_dark else 'rgba(255, 160, 0, 0.15)')
                    .replace('BG_COLOR', 'rgba(255, 255, 255, 0.15)' if is_dark else 'rgba(0, 0, 0, 0.08)')
                    .replace('BORDER_COLOR', 'rgba(255, 255, 255, 0.1)' if is_dark else 'rgba(0, 0, 0, 0.1)')
                )
                app.setStyleSheet(app.styleSheet() + css)
            return True
        elif theme_name.endswith('.qss'):
            theme_path = config.THEMES_DIR / theme_name
            if theme_path.exists():
                app.setStyleSheet(theme_path.read_text(encoding='utf-8'))
                return True
            else:
                print(f"Warning: Custom theme file not found at {theme_path}")
                return False
        return False

    try:
        success = apply_initial_theme(initial_theme)
        if not success and initial_theme != default_theme:
            print(f"Falling back to default theme: {default_theme}")
            apply_initial_theme(default_theme)
    except Exception as e:
        print(f'Failed to load theme: {e}')
        if initial_theme != default_theme:
            apply_stylesheet(app, theme=default_theme, extra={'density_scale': '0'})

    update_info = {"update_available": False}

    if len(sys.argv) > 1:
        from cli import cli_mode

        if update_info and update_info.get("update_available"):
            print(f"[INFO] A new version (v{update_info['latest_version']}) is available. Run 'update' in interactive mode for details.\n")

        cli_mode(update_info)
    else:
        if sys.platform == 'darwin':
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                pass
            multiprocessing.freeze_support()

        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel
        from PySide6.QtCore import QTimer

        splash = QDialog(None, Qt.FramelessWindowHint)
        splash.setObjectName('GlassSplash')
        splash.setFixedSize(450, 320)

        if initial_theme.endswith('.xml'):
            pal = app.palette()
            bg_color = pal.color(QPalette.Window).name()
            border_color = pal.color(QPalette.Highlight).name()
            text_color = pal.color(QPalette.WindowText).name()

            splash.setStyleSheet(f"""
                QDialog#GlassSplash {{
                    background-color: {bg_color};
                    border: 2px solid {border_color};
                    border-radius: 12px;
                }}
                QLabel#SplashStatus {{
                    color: {text_color};
                    font-size: 10pt;
                    margin-top: 15px;
                }}
            """)
        splash_layout = QVBoxLayout(splash)
        splash_layout.setAlignment(Qt.AlignCenter)
        splash_layout.setContentsMargins(30, 40, 30, 30)

        pixmap = QPixmap(str(config.text_logo)).scaled(
            320, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        logo_label = QLabel()
        logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignCenter)
        splash_layout.addWidget(logo_label)

        status_label = QLabel('Starting Scene Scout GUI...')
        status_label.setObjectName('SplashStatus')
        status_label.setAlignment(Qt.AlignCenter)
        splash_layout.addWidget(status_label)

        splash.show()
        app.processEvents()

        status_label.setText('Loading libraries...')
        app.processEvents()

        from gui import SceneScoutApp

        status_label.setText('Building interface...')
        app.processEvents()

        main_window = SceneScoutApp()

        main_window.showMaximized()
        splash.accept()
        splash.deleteLater()

        QTimer.singleShot(100, main_window.threaded_load_model)

        sys.exit(app.exec())
