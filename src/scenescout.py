import sys
from update_checker import check_for_update
import multiprocessing

from PySide6.QtGui import QPixmap, QPalette
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qt_material import apply_stylesheet

import config

if __name__ == '__main__':
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)

    saved_config = config.load_config()
    initial_theme = saved_config.get('theme', 'dark_teal.xml')
    try:
        if initial_theme.endswith('.xml'):
            apply_stylesheet(app, theme=initial_theme, extra={'density_scale': '0'})
        elif initial_theme.endswith('.qss'):
            theme_path = config.THEMES_DIR / initial_theme
            if theme_path.exists():
                with open(theme_path, "r", encoding="utf-8") as f:
                    app.setStyleSheet(f.read())
            else:
                print(f"Warning: Custom theme file not found at {theme_path}")
    except Exception as e:
        print(f'Failed to load theme: {e}')

    update_info = check_for_update()

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

        if update_info and update_info.get("update_available"):
            from gui_utils import show_update_dialog
            QTimer.singleShot(500, lambda: show_update_dialog(main_window, update_info))

        sys.exit(app.exec())
