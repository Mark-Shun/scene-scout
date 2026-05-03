import os
import sys
import tkinter as tk
from PIL import Image, ImageTk

def load_app_icon(master_window, icon_path):
    """Loads and resizes the application icon, returning the ImageTk object."""
    try:
        # Resize for compatibility across taskbars and window titles
        icon_img = Image.open(icon_path).resize((64, 64), Image.Resampling.LANCZOS)
        icon_photo = ImageTk.PhotoImage(icon_img, master=master_window)
        
        # Apply globally to root
        master_window.iconphoto(True, icon_photo)
        
        # Native Windows optimization
        if sys.platform == 'win32':
            ico_path = icon_path.with_suffix('.ico')
            if os.path.exists(ico_path):
                master_window.iconbitmap(ico_path)
                
        return icon_photo
    except Exception as e:
        print(f"Icon Load Warning: {e}")
        return None

def apply_window_icon(window, icon_obj):
    """Manually applies the icon to Toplevel windows for cross-platform stability."""
    if icon_obj:
        window.iconphoto(False, icon_obj)

def center_window(window, width, height):
    """Centers a window or dialog on the screen."""
    window.update_idletasks()
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    x = (sw - width) // 2
    y = (sh - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")