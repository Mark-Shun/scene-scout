import sys
from tkinter import messagebox

import config
from cli import cli_mode
from gui import SceneScoutApp, show_splash
from update_checker import check_for_update

if __name__ == '__main__':
    check_for_update()

    if len(sys.argv) > 1:
        cli_mode()
    else:
        splash, splash_root = show_splash()
        splash.status_label.config(text="Starting Scene Scout GUI")
        splash.update()

        app = SceneScoutApp(splash_ref=splash)

        try:
            app.load_model()
            app.on_model_load_finished()

        except Exception as e:
            messagebox.showerror('Model Error', f'Failed to load model: {e}')
            app.update_status('Error loading model.')

        splash.destroy()
        app.splash_ref = None

        try:
            splash_root.destroy()
        except Exception:
            pass
        
        app.mainloop()