import sys
from update_checker import check_for_update
import multiprocessing

if __name__ == '__main__':
    update_info = check_for_update()

    if len(sys.argv) > 1:
        from cli import cli_mode
        
        # Minimal boot notification
        if update_info and update_info.get("update_available"):
            print(f"[INFO] A new version (v{update_info['latest_version']}) is available. Run 'update' in interactive mode for details.\n")
            
        cli_mode(update_info)
    else:
        # GUI Mode
        if sys.platform == 'darwin':
            try:
                multiprocessing.set_start_method('spawn', force=True)
            except RuntimeError:
                pass
            multiprocessing.freeze_support()

        from tkinter import messagebox
        from gui import SceneScoutApp, show_splash
        from gui_utils import show_update_dialog
        
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
            
        # Trigger the update dialog after the main window is rendered
        if update_info and update_info.get("update_available"):
            app.after(500, lambda: show_update_dialog(app, update_info))
        
        app.mainloop()