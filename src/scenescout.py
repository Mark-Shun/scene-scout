import sys
from update_checker import check_for_update
import multiprocessing

if __name__ == '__main__':
    check_for_update()

    if len(sys.argv) > 1:
        from cli import cli_mode
        cli_mode()
    else:
        if sys.platform == 'darwin':
            # Initialize multiprocessing for macOS stability
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                pass
            multiprocessing.freeze_support()

        from tkinter import messagebox
        from gui import SceneScoutApp, show_splash
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