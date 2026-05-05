import os
import sys
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import webbrowser
import re


class ToolTip:
    """Creates a tooltip for a given widget."""
    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._tipwindow = None
        self.widget.bind('<Enter>', self._schedule)
        self.widget.bind('<Leave>', self._hide)
        self.widget.bind('<Motion>', self._move)

    def _schedule(self, event=None):
        self._unschedule()
        self._after_id = self.widget.after(self.delay, self._show)

    def _unschedule(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self._tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        label = ttk.Label(tw, text=self.text, background='#ffffe0', relief='solid', borderwidth=1, wraplength=240)
        label.pack(ipadx=6, ipady=3)

    def _move(self, event=None):
        if self._tipwindow:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
            self._tipwindow.wm_geometry(f'+{x}+{y}')

    def _hide(self, event=None):
        self._unschedule()
        if self._tipwindow:
            self._tipwindow.destroy()
            self._tipwindow = None


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

def insert_markdown(text_widget, text):
    """A lightweight Markdown parser for tk.Text widgets."""
    
    # 1. Pre-process text to strip markdown artifacts
    # Strip images entirely: ![alt text](url) -> ""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # Strip links but keep the display text: [display text](url) -> display text
    text = re.sub(r'\[([^\]]+)\]\(.*?\)', r'\1', text)
    
    # 2. Define text styles (tags)
    # Using Tkinter's cross-platform built-in fonts ensures it blends with any theme
    base_font = "TkDefaultFont"
    mono_font = "TkFixedFont"
    
    # Headings (with spacing above and below)
    text_widget.tag_configure('h1', font=(base_font, 14, 'bold'), spacing1=15, spacing3=5)
    text_widget.tag_configure('h2', font=(base_font, 12, 'bold'), spacing1=10, spacing3=5)
    text_widget.tag_configure('h3', font=(base_font, 11, 'bold'), spacing1=10, spacing3=3)
    
    # Inline formatting
    text_widget.tag_configure('bold', font=(base_font, 9, 'bold'))
    text_widget.tag_configure('code', font=(mono_font, 9)) 
    
    # lmargin1 is the first line, lmargin2 is where wrapped text aligns
    text_widget.tag_configure('bullet', lmargin1=15, lmargin2=30, spacing1=2, spacing3=2)
    text_widget.tag_configure('normal', lmargin1=5, lmargin2=5, spacing1=2, spacing3=2)

    lines = text.split('\n')
    for line in lines:
        line_stripped = line.strip()
        
        # Handle empty lines
        if not line_stripped:
            text_widget.insert('end', '\n', ('normal',))
            continue
            
        line_tags = tuple()
        
        # Parse Headers
        if line_stripped.startswith('### '):
            text_widget.insert('end', line_stripped[4:] + '\n', ('h3',))
            continue
        elif line_stripped.startswith('## '):
            text_widget.insert('end', line_stripped[3:] + '\n', ('h2',))
            continue
        elif line_stripped.startswith('# '):
            text_widget.insert('end', line_stripped[2:] + '\n', ('h1',))
            continue
        
        # Parse Bullet Points
        if line_stripped.startswith('- ') or line_stripped.startswith('* '):
            line = '• ' + line_stripped[2:]
            line_tags = ('bullet',)
        else:
            line_tags = ('normal',)
        
        # Parse Inline Markdown (Bold and Code blocks)
        # Regex splits the line while keeping the delimiters so we can identify them
        parts = re.split(r'(\*\*.*?\*\*|`.*?`)', line)
        
        for part in parts:
            if not part:
                continue
            if part.startswith('**') and part.endswith('**'):
                # Apply bold tag (strip the ** asterisks)
                tags = line_tags + ('bold',)
                text_widget.insert('end', part[2:-2], tags)
            elif part.startswith('`') and part.endswith('`'):
                # Apply code tag (strip the ` backticks)
                tags = line_tags + ('code',)
                text_widget.insert('end', part[1:-1], tags)
            else:
                # Standard text
                text_widget.insert('end', part, line_tags)
        
        text_widget.insert('end', '\n', line_tags)

def show_update_dialog(parent_app, update_info):
    """Displays a non-blocking dialog with formatted update details."""
    dlg = tk.Toplevel(parent_app)
    apply_window_icon(dlg, parent_app.app_icon)
    dlg.title("Update Available")
    dlg.transient(parent_app)
    dlg.grab_set()
    
    main_frame = ttk.Frame(dlg, padding=20)
    main_frame.pack(fill='both', expand=True)
    
    # Header
    ttk.Label(main_frame, text="A new version of Scene Scout is available!", font=('', 12, 'bold')).pack(anchor='w', pady=(0, 10))
    ttk.Label(main_frame, text=f"Current: v{update_info['current_version']}  ➔  Latest: v{update_info['latest_version']}").pack(anchor='w', pady=(0, 15))
    
    # Release Notes Text Area (Scrollable)
    notes_frame = ttk.LabelFrame(main_frame, text="Release Notes", padding=5)
    notes_frame.pack(fill='both', expand=True, pady=(0, 15))
    
    # Standard tk.Text widget with wrapped words
    text_area = tk.Text(notes_frame, wrap='word', height=14, width=70, bg=parent_app.cget('bg'), relief='flat', font=("TkDefaultFont", 9))
    scrollbar = ttk.Scrollbar(notes_frame, command=text_area.yview)
    text_area.configure(yscrollcommand=scrollbar.set)
    
    text_area.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')
    
    # Insert the formatted Markdown
    insert_markdown(text_area, update_info['notes'])
    
    # Disable text area so the user can't type in it
    text_area.configure(state='disabled')
    
    # Action Buttons
    btn_frame = ttk.Frame(main_frame)
    btn_frame.pack(fill='x')
    
    download_btn = ttk.Button(btn_frame, text="Open Release Page", command=lambda: webbrowser.open_new(update_info['url']))
    download_btn.pack(side='left', padx=(0, 5))
    
    close_btn = ttk.Button(btn_frame, text="Close", command=dlg.destroy)
    close_btn.pack(side='right')
    
    center_window(dlg, 600, 500)