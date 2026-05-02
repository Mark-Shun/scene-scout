import json
import os
import sys
import threading
import tkinter as tk
import subprocess
import webbrowser
import sqlite3
from model_loader import load_siglip_model
from tkinter import filedialog, messagebox, ttk, simpledialog
from typing import Callable, List, Optional, Tuple
from ttkthemes import ThemedStyle
from tkinterdnd2 import DND_FILES, TkinterDnD
from pathlib import Path

import av
import numpy as np
import torch
try:
    import torch_directml
except ImportError:
    torch_directml = None

try:
    import intel_extension_for_pytorch as ipex # Required for Intel XPU
except ImportError:
    ipex = None

try:
    import vlc
except Exception:
    import traceback
    traceback.print_exc()
    print('A VLC installation is needed for the GUI. Please install VLC before starting Scene Scout.', file=sys.stderr)
    sys.exit(1)

from PIL import Image, ImageTk
from transformers import AutoProcessor, Siglip2Model
from model_loader import get_compute_device
from database import init_db, db_is_empty, cleanup_orphaned_entries, search_scenes, search_db
from processing import index_files, get_query_embedding
import config

big_logo = os.path.join(os.path.dirname(__file__),"../", "assets", "logo", "scene-scout-logo.png")
text_logo = os.path.join(os.path.dirname(__file__),"../", "assets", "logo", "scene-scout-text-logo.png")
scene_scout_logo = Image.open(big_logo)

def show_splash():
    root = tk.Tk()
    root.withdraw()

    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    
    # Load and resize logo
    img = Image.open(text_logo)
    logo_w, logo_h = img.size
    scale = 400 / logo_w
    new_w, new_h = int(logo_w * scale), int(logo_h * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    tk_img = ImageTk.PhotoImage(img, master=splash)
    
    # Image Label
    label = tk.Label(splash, image=tk_img)
    label.image = tk_img
    label.pack()

    # Attach to splash object so it can be referenced later
    splash.status_label = tk.Label(
        splash, 
        text="Initializing...", 
        font=("Arial", 10), 
        pady=10,
        fg="#333333"
    )
    splash.status_label.pack(fill='x')
    
    # Calculate center position including the new label height
    splash.update_idletasks()
    w, h = splash.winfo_reqwidth(), splash.winfo_reqheight()
    sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    splash.geometry(f"{w}x{h}+{x}+{y}")
    
    splash.lift()
    return splash, root

class SceneScoutApp(TkinterDnD.Tk):

    def __init__(self, splash_ref=None):
        super().__init__()
        self.title('Scene Scout')
        self.splash_ref = splash_ref
        self.config = config.load_config()
        screen_width = (self.winfo_screenwidth())-200
        screen_height = (self.winfo_screenheight())-200
        self.geometry(f"{screen_width}x{screen_height}+0+0")
        self.model: Optional[Siglip2Model] = None
        self.processor = None
        self.device: Optional[torch.device] = None
        self.dtype: Optional[torch.dtype] = None
        self.db_path: Optional[str] = None
        self.query_image_path: Optional[str] = None
        self.current_display_path: Optional[str] = None
        self.search_results: List[Tuple[str, float, str, Optional[float], Optional[int], Optional[int], Optional[int]]] = []
        self.use_trt_var = tk.BooleanVar(master=self, value=self.config.get('use_trt', False))
        self.use_trt_var.trace_add("write", lambda *args: self.save_trt_preference())
        self.use_vlc_open_var = tk.BooleanVar(
            master=self, 
            value=self.config.get('use_vlc_open', True)
        )
        self.video_cap = None
        self.video_loop_id = None
        self.loop_start_ms: Optional[int] = None
        self.loop_end_ms: Optional[int] = None
        self.loop_start_ms: Optional[int] = None
        self.loop_end_ms: Optional[int] = None
        self.last_selected_entry = None
        self.canvas_scale = 1.0
        self._loop_scale_set = False
        self.canvas_offset_x, self.canvas_offset_y = (0, 0)
        self.drag_start_x, self.drag_start_y = (0, 0)
        self.original_image: Optional[Image.Image] = None
        self.display_image: Optional[Image.Image] = None
        self.tk_image: Optional[ImageTk.PhotoImage] = None
        self.zoom_timer: Optional[str] = None
        self.style = ThemedStyle(self)
        self.current_theme = self.config.get("theme", "radiance")
        available_themes = self.style.theme_names()
        if self.current_theme in available_themes:
            self.style.theme_use(self.current_theme)
        else:
            self.style.theme_use("radiance")
            self.current_theme = "radiance"

        self.theme_var = tk.StringVar(master=self, value=self.current_theme)

        vlc_args = config.get_vlc_args()
        self.vlc_instance = vlc.Instance(* vlc_args)
        self.player = self.vlc_instance.media_player_new()
        self.vlc_events = self.player.event_manager()
        self.vlc_events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end_reached)

        device_str, _, _, _ = get_compute_device(self.config.get("device"))
        from model_loader import TRT_AVAILABLE
        self.show_trt_option = (device_str == 'cuda' and TRT_AVAILABLE)

        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', self.on_handle_drop)

        self.setup_widgets()
        self.load_saved_paths()
        image = Image.open(big_logo)
        self.icon = ImageTk.PhotoImage(image, master=self)
        self.iconphoto(True, self.icon)

        # when starting, lock interaction until model is ready
        self.set_controls_enabled(False)
        self.withdraw()


    def setup_widgets(self):
        # 1. Main Layout Containers
        mainframe = ttk.Frame(self, padding='10')
        mainframe.pack(fill='both', expand=True)
        
        main_paned = ttk.PanedWindow(mainframe, orient='horizontal')
        main_paned.pack(fill='both', expand=True)

        # --- LEFT SIDE: Scrollable Controls Pane ---
        controls_pane = ttk.Frame(main_paned)
        main_paned.add(controls_pane, weight=0)

        # Scrollbar and Canvas setup
        canvas = tk.Canvas(controls_pane, highlightthickness=0, width=320)
        scrollbar = ttk.Scrollbar(controls_pane, orient="vertical", command=canvas.yview)
        
        # The frame that actually holds the widgets
        self.scrollable_controls = ttk.Frame(canvas, padding="10")
        
        # Inner window setup
        window_id = canvas.create_window((0, 0), window=self.scrollable_controls, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Layout management bindings
        def _on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(window_id, width=event.width)

        self.scrollable_controls.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mousewheel support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 2. Database Section
        db_frame = ttk.LabelFrame(self.scrollable_controls, text='Database', padding=5)
        db_frame.pack(fill='x', pady=5)
        self.db_var = tk.StringVar(master=self, value='No database selected')
        ttk.Label(db_frame, textvariable=self.db_var, wraplength=250).pack(anchor='w')
        
        db_btn_frame = ttk.Frame(db_frame)
        db_btn_frame.pack(fill='x', pady=2)
        ttk.Button(db_btn_frame, text='Open Existing...', command=self.browse_existing_database).pack(side='left', expand=True, fill='x', padx=(0, 2))
        ttk.Button(db_btn_frame, text='Create New...', command=self.browse_database).pack(side='left', expand=True, fill='x', padx=(2, 0))

        # 3. Folder Section
        folder_frame = ttk.LabelFrame(self.scrollable_controls, text='Folder to process', padding=5)
        folder_frame.pack(fill='x', pady=5)
        self.folder_var = tk.StringVar(master=self)
        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.pack(fill='x', pady=2)
        ttk.Button(folder_frame, text='Select Folder', command=self.browse_folder).pack(fill='x')
        self.index_button = ttk.Button(folder_frame, text='Process Media In Folder', command=self.threaded_index, state='disabled')
        self.index_button.pack(fill='x', pady=3)

        # 4. Search Query Section
        query_frame = ttk.LabelFrame(self.scrollable_controls, text='Search Query', padding=5)
        query_frame.pack(fill='x', pady=5)
        ttk.Label(query_frame, text='Text:').pack(anchor='w')
        self.query_text_var = tk.StringVar(master=self)
        self.query_text_entry = ttk.Entry(query_frame, textvariable=self.query_text_var)
        self.query_text_entry.pack(fill='x', pady=(0, 5))
        self.query_text_entry.bind('<Return>', lambda e: self.threaded_search())
        
        ttk.Label(query_frame, text='Image:').pack(anchor='w')
        self.query_image_var = tk.StringVar(master=self, value='No query image')
        ttk.Label(query_frame, textvariable=self.query_image_var, wraplength=250).pack(anchor='w')
        
        btn_frame = ttk.Frame(query_frame)
        btn_frame.pack(fill='x', pady=2)
        ttk.Button(btn_frame, text='Load...', command=self.browse_query_image).pack(side='left', expand=True, fill='x')
        ttk.Button(btn_frame, text='Clear', command=self.clear_query_image).pack(side='left', expand=True, fill='x')

        self.search_button = ttk.Button(query_frame, text='Search Scene', command=self.threaded_search, state='disabled')
        self.search_button.pack(fill='x', pady=3)

        # 5. Options Section
        options_frame = ttk.LabelFrame(self.scrollable_controls, text='Options', padding=5)
        options_frame.pack(fill='x', pady=5)
        
        # GPU device or CPU detection
        device_str, device_msg, _, _ = get_compute_device()
        ttk.Label(options_frame, text='Compute Device:').pack(anchor='w', pady=(5, 0))
        self.device_var = tk.StringVar(master=self, value=device_str)
        
        # Build available options dynamically
        device_options = ['cpu']
        if torch.cuda.is_available():
            device_options.append('cuda')
        if torch_directml is not None:
            device_options.append('dml')
            
        # Ensure the default is in the list, otherwise fallback to CPU
        if self.device_var.get() not in device_options:
            self.device_var.set('cpu')
            
        self.device_combobox = ttk.Combobox(
            options_frame, 
            textvariable=self.device_var, 
            values=device_options, 
            state='readonly'
        )
        self.device_combobox.pack(fill='x')
        ttk.Label(options_frame, text=f'Auto-detected: {device_msg}', font=('', 8, 'italic')).pack(anchor='w')

        # Tensor RT option
        if self.show_trt_option:
            self.trt_check = ttk.Checkbutton(
                options_frame,
                text='Use TensorRT Acceleration', 
                variable=self.use_trt_var,
                onvalue=True, 
                offvalue=False,
                command=self.save_trt_preference
            )
            self.trt_check.pack(anchor='w', pady=(5, 0))

        # Detection Method Toggle (Replaces Checkbox)
        ttk.Label(options_frame, text='Detection method:').pack(anchor='w', pady=(5, 0))
        detect_method_frame = ttk.Frame(options_frame)
        detect_method_frame.pack(fill='x', pady=5)
        self.fast_detect_var = tk.BooleanVar(master=self, value=True)

        ttk.Radiobutton(detect_method_frame, text='Fast', variable=self.fast_detect_var, 
                        value=True, style='Toolbutton').pack(side='left', expand=True, fill='x', padx=(0, 2))
        ttk.Radiobutton(detect_method_frame, text='Accurate', variable=self.fast_detect_var, 
                        value=False, style='Toolbutton').pack(side='left', expand=True, fill='x', padx=(2, 0))

        ttk.Label(options_frame, text='Max Patches:').pack(anchor='w', pady=(5, 0))
        self.max_patches_var = tk.IntVar(master=self, value=256)
        ttk.Spinbox(options_frame, from_=128, to=1024, increment=128, textvariable=self.max_patches_var).pack(fill='x')
        
        ttk.Label(options_frame, text='Results:').pack(anchor='w', pady=(5, 0))
        self.top_k_var = tk.IntVar(master=self, value=20)
        ttk.Spinbox(options_frame, from_=1, to=100, textvariable=self.top_k_var).pack(fill='x')
        
        ttk.Label(options_frame, text='Scene embed batch size:').pack(anchor='w', pady=(5, 0))
        self.input_batch_size = tk.IntVar(master=self, value=16)
        ttk.Spinbox(options_frame, from_=8, to=160, textvariable=self.input_batch_size).pack(fill='x')

        self.vlc_open_check = ttk.Checkbutton(
            options_frame, 
            text='Open video in VLC', 
            variable=self.use_vlc_open_var,
            command=self.save_vlc_preference
        )
        self.vlc_open_check.pack(anchor='w', pady=(5, 0))

        # Theme selection
        ttk.Label(options_frame, text='Theme:').pack(anchor='w', pady=(5, 0))
        theme_frame = ttk.Frame(options_frame)
        theme_frame.pack(fill='x', pady=(0, 5))
        available_themes = sorted(self.style.theme_names())
        self.theme_combobox = ttk.Combobox(
            theme_frame,
            textvariable=self.theme_var,
            values=available_themes,
            state='readonly'
        )
        self.theme_combobox.pack(side='left', fill='x', expand=True)
        ttk.Button(
            theme_frame,
            text='Apply',
            command=self.apply_theme
        ).pack(side='left', padx=(5, 0))

        # 6. Additional actions Section
        actions_frame = ttk.LabelFrame(self.scrollable_controls, text='Additional Actions', padding=5)
        actions_frame.pack(fill='x', pady=10)
        self.load_model_button = ttk.Button(actions_frame, text='Load Model', command=self.threaded_load_model)
        self.load_model_button.pack(fill='x', pady=3)
        ttk.Button(actions_frame, text='Cleanup Database', command=self.cleanup_database).pack(fill='x', pady=3)

        # Info Section
        info_frame = ttk.LabelFrame(self.scrollable_controls, text='Info', padding=5)
        info_frame.pack(fill='x', pady=(0,10))
        ttk.Button(info_frame, text='About', command=self.open_about_dialog).pack(fill='x')

        self.status_var = tk.StringVar(master=self, value='Select database and load model')
        ttk.Label(self.scrollable_controls, textvariable=self.status_var, wraplength=280).pack(side='bottom', fill='x', pady=10)

        # --- RIGHT SIDE: Results and Preview ---
        results_frame = ttk.LabelFrame(main_paned, text='Results', padding='10')
        main_paned.add(results_frame, weight=1)
        
        paned_window = ttk.PanedWindow(results_frame, orient='horizontal')
        paned_window.pack(fill='both', expand=True)
        
        list_frame = ttk.Frame(paned_window)
        self.stats_label = ttk.Label(list_frame, text='No search performed')
        self.stats_label.pack(anchor='w')
        
        rescore_frame = ttk.Frame(list_frame)
        rescore_frame.pack(fill='x', pady=5)
        self.rescore_button = ttk.Button(rescore_frame, text='Rescore...', command=self.open_rescore_dialog, state='disabled')
        self.rescore_button.pack(side='left')
        self.clear_rescore_button = ttk.Button(rescore_frame, text='Clear Rescore', command=self.clear_rescore, state='disabled')
        self.clear_rescore_button.pack(side='left', padx=5)
        
        self.results_tree = ttk.Treeview(list_frame, columns=('filename','scene','time','score','rescore'), show='headings', selectmode='browse')
        for col, width in zip(['filename', 'scene', 'time', 'score', 'rescore'], [300, 80, 150, 80, 80]):
            self.results_tree.heading(col, text=col.capitalize())
            self.results_tree.column(col, width=width, anchor='center' if col != 'filename' else 'w')
        
        self.results_tree.pack(side='left', fill='both', expand=True)
        list_scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.results_tree.yview)
        list_scrollbar.pack(side='right', fill='y')
        self.results_tree.config(yscrollcommand=list_scrollbar.set)
        self.results_tree.bind('<<TreeviewSelect>>', self.on_result_select)
        self.results_tree.bind('<Double-1>', self.on_result_double_click)
        self.results_tree.bind('<Button-3>', self.on_results_right_click)
        
        paned_window.add(list_frame, weight=1)

        preview_frame = ttk.Frame(paned_window)
        playback_state = 'On' if config.SCENE_PLAYBACK else 'Off'
        self._playback_toggle_btn = ttk.Button(preview_frame, text=f'Toggle preview playback ({playback_state})', command=self.toggle_preview_playback)
        self._playback_toggle_btn.pack(fill='x', pady=(0,5))
        
        self.image_canvas = tk.Canvas(preview_frame, bg='gray')
        self.image_canvas.pack(fill='both', expand=True)
        
        self.video_container = ttk.Frame(self.image_canvas)
        self.video_container.pack(fill='both', expand=True)
        self.video_container.bind("<Configure>", self.on_player_resize)
        
        paned_window.add(preview_frame, weight=1)

    def on_handle_drop(self, event):
        # tkinterdnd2 returns paths in a specific format (spaces are handled with braces)
        paths = self.tk.splitlist(event.data)
        if not paths:
            return

        path = paths[0] # Handle the first item dropped
        
        # 1. Check if it is a Folder
        if os.path.isdir(path):
            self.folder_var.set(path)
            self.config['folder_path'] = path
            config.save_config(self.config)
            self.update_status(f"Folder set via drop: {os.path.basename(path)}")
            return

        # 2. Check if it is a Database file
        if path.lower().endswith('.db'):
            self.db_path = path
            self.db_var.set(os.path.basename(path))
            init_db(self.db_path)
            self.config['db_path'] = path
            config.save_config(self.config)
            self.update_status(f"Database loaded via drop: {os.path.basename(path)}")
            return

        # 3. Check if it is an Image file for query
        if path.lower().endswith(config.IMAGE_EXTENSIONS):
            self.query_image_path = path
            self.query_image_var.set(os.path.basename(path))
            self.update_status(f"Query image set via drop: {os.path.basename(path)}")
            return
            
        self.update_status("Unsupported file type dropped.")

    def threaded_task(self, target_func: Callable, *args):
        thread = threading.Thread(target=target_func, args=args, daemon=True)
        thread.start()

    def set_controls_enabled(self, enabled: bool):
        state = 'normal' if enabled else 'disabled'
        for name in ['load_model_button', 'index_button', 'search_button', 'rescore_button', 'clear_rescore_button', 'query_text_entry', 'folder_entry']:
            widget = getattr(self, name, None)
            if widget:
                try:
                    widget.config(state=state)
                except Exception:
                    pass
    
    def save_trt_preference(self):
        """Save the TensorRT preference to the GUI config file."""
        self.config['use_trt'] = self.use_trt_var.get()
        config.save_config(self.config)

    def save_vlc_preference(self):
        """Save the VLC opening preference to the config file."""
        self.config['use_vlc_open'] = self.use_vlc_open_var.get()
        config.save_config(self.config)

    def update_status(self, message: str):
        """Thread-safe wrapper to schedule UI updates on the main thread."""
        self.after(0, self._update_status_ui, message)

    def _update_status_ui(self, message: str):
        """Actual UI update logic that runs safely on the main loop."""
        self.status_var.set(message)
        if self.splash_ref and self.splash_ref.winfo_exists():
            if hasattr(self.splash_ref, 'status_label'):
                try:
                    self.splash_ref.status_label.config(text=message)
                    self.splash_ref.update()
                except (tk.TclError, AttributeError):
                    pass
        self.update_idletasks()

    def load_saved_paths(self):
        if 'db_path' in self.config and os.path.exists(self.config['db_path']):
            self.db_path = self.config['db_path']
            assert self.db_path is not None
            self.db_var.set(os.path.basename(self.db_path))
            init_db(self.db_path)
            self.update_status(f'Loaded saved database: {os.path.basename(self.db_path)}')
        if 'folder_path' in self.config and os.path.exists(self.config['folder_path']):
            self.folder_var.set(self.config['folder_path'])

    def browse_database(self):
        path = filedialog.asksaveasfilename(title='Create New Database File', initialdir=self.config.get('db_path', ''), filetypes=[('SQLite Database', '*.db')], defaultextension='.db')
        if path:
            self.db_path = path
            self.db_var.set(os.path.basename(path))
            init_db(self.db_path)
            self.config['db_path'] = path
            config.save_config(self.config)
            self.update_status(f'Database set to: {os.path.basename(path)}')
            if not Path(path).parent.is_dir():
                return
            self.folder_var.set(str(Path(path).parent))
            self.config['folder_path'] = str(Path(path).parent)
            config.save_config(self.config)

    def browse_existing_database(self):
        path = filedialog.askopenfilename(title='Select Existing Database File', initialdir=self.config.get('db_path', ''), filetypes=[('SQLite Database', '*.db')])
        if path:
            self.db_path = path
            self.db_var.set(os.path.basename(path))
            init_db(self.db_path)
            self.config['db_path'] = path
            config.save_config(self.config)
            self.update_status(f'Database set to: {os.path.basename(path)}')

    def browse_folder(self):
        path = filedialog.askdirectory(title='Select Folder to Index', initialdir=self.config.get('folder_path', ''))
        if path:
            self.folder_var.set(path)
            self.config['folder_path'] = path
            config.save_config(self.config)

    def browse_query_image(self):
        path = filedialog.askopenfilename(filetypes=[('Images', ' '.join((f'*{ext}' for ext in config.IMAGE_EXTENSIONS)))])
        if path:
            self.query_image_path = path
            self.query_image_var.set(os.path.basename(path))

    def clear_query_image(self):
        self.query_image_path = None
        self.query_image_var.set('No query image')

    def open_about_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title('About Scene Scout')
        dlg.transient(self)
        dlg.resizable(False, False)

        # Center the dialog over parent
        dlg.update_idletasks()
        w, h = 400, 260
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        frame = ttk.Frame(dlg, padding=10)
        frame.pack(fill='both', expand=True)

        text = tk.Text(frame, wrap='word', height=10)
        text.pack(fill='both', expand=True)

        about_text = (
            "Scene Scout\n\n"
            "Scene scout is a tool written to help with searching for specific scenes using keywords."
            "It is forked and build on top of Gabrjiele project and uses Google's Siglip2 for embedding- and extracting the visual information.\n\n"
            "Made by: Mark-Shun/Sonicfreak\n"
            "Logo made by Miwo: https://4miwo.carrd.co\n"
            "Project site: https://github.com/Mark-Shun/scene-scout\n"
            "Original source: https://github.com/Gabrjiele/siglip2-naflex-search\n"
        )
        text.insert('1.0', about_text)

        # make links clickable
        def _make_link(tag_start, tag_end, url):
            text.tag_add(url, tag_start, tag_end)
            text.tag_config(url, foreground='blue', underline=True)
            def _open(event, target=url):
                webbrowser.open_new(target)
            text.tag_bind(url, '<Button-1>', _open)

        # find URLs in the inserted text and tag them
        for url in ['https://4miwo.carrd.co/', 'https://github.com/Mark-Shun/scene-scout', 'https://github.com/Gabrjiele/siglip2-naflex-search']:
            idx = text.search(url, '1.0', tk.END)
            if idx:
                end = f"{idx}+{len(url)}c"
                _make_link(idx, end, url)

        text.config(state='disabled')

        btn = ttk.Button(frame, text='Close', command=dlg.destroy)
        btn.pack(pady=(8, 0))

    def cleanup_database(self):
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        assert self.db_path is not None
        if messagebox.askyesno('Confirm', 'Remove entries for deleted files from the database?'):
            self.threaded_task(self._cleanup_task)

    def _cleanup_task(self):
        assert self.db_path is not None
        self.update_status('Cleaning up database...')
        count = cleanup_orphaned_entries(self.db_path, self.update_status)
        self.after(0, lambda: messagebox.showinfo('Complete', f'Removed {count} orphaned embeddings.'))
        self.update_status('Cleanup complete.')

    def load_model(self, device_choice=None, use_trt=None):
        """
        Loads the model. If no arguments are passed, it defaults F
        to the current UI variables.
        """
        # Fallback to the current UI values if nothing is passed
        if device_choice is None:
            device_choice = self.device_var.get()
        if use_trt is None:
            use_trt = self.use_trt_var.get()
        
        # Store results in instance variables (not Tkinter variables)
        self.model, self.processor, self.device, self.dtype, self._last_active_device = load_siglip_model(
            device_choice, 
            status_callback=self.update_status,
            use_trt=use_trt
        )

    def threaded_load_model(self):
        """Captures UI state on the main thread before starting the background task."""
        self.load_model_button.config(state='disabled')
        self.index_button.config(state='disabled')
        self.search_button.config(state='disabled')
        
        # CAPTURE VALUES HERE (Main Thread)
        device_choice = self.device_var.get()
        use_trt = self.use_trt_var.get()
        
        self.update_status(f'Loading model: {config.DEFAULT_MODEL}...')
        
        # Pass the captured values to the task
        self.threaded_task(self.load_model_task, device_choice, use_trt)

    def load_model_task(self, device_choice, use_trt):
        """Background task runner."""
        try:
            # Pass values through to load_model
            self.load_model(device_choice=device_choice, use_trt=use_trt)
            self.after(0, self.on_model_load_finished)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('Model Error', f'Failed to load model: {e}'))
            self.after(0, lambda: self.load_model_button.config(state='normal'))
            self.after(0, lambda: self.update_status('Error loading model.'))

    def on_model_load_finished(self):
        """Updates the GUI once the background loading thread is complete."""
        # Now safely update the Tkinter variable on the main thread
        if hasattr(self, '_last_active_device') and hasattr(self, 'device_var'):
            self.device_var.set(self._last_active_device)

        self.update_status(f'Model loaded on {str(self.device).upper()}. Ready!')
        self.set_controls_enabled(True)
        self.load_model_button.config(text='Reload Model')
        if not self.winfo_viewable():
            self.deiconify()

    def threaded_index(self):
        if not self.db_path or not self.folder_var.get():
            messagebox.showerror('Error', 'Please select a database and a folder to index.')
            return
        self.index_button.config(state='disabled')
        self.search_button.config(state='disabled')
        self._cancel_event = threading.Event()
        self.show_indexing_popup()
        self.update_status('Indexing in progress...')
        self.threaded_task(self.index_task, self.folder_var.get())

    def show_indexing_popup(self):
        self.index_popup = tk.Toplevel(self)
        self.index_popup.title('Indexing files')
        self.index_popup.transient(self)
        self.index_popup.grab_set()
        self.index_popup.minsize(500, 230)
        
        frame = ttk.Frame(self.index_popup, padding=20)
        frame.pack(fill='both', expand=True)
        
        # Label for the current file name
        self.index_filename_var = tk.StringVar(master=self.index_popup, value='Initializing...')
        ttk.Label(frame, textvariable=self.index_filename_var, font=('Arial', 10, 'bold'), wraplength=400).pack(pady=(0, 10))
        
        # Progress Bar (linked to DoubleVar)
        self.progress_var = tk.DoubleVar(master=self.index_popup, value=0)
        self.progress_bar = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', pady=5)
        
        # Status/Stats Label (adds whitespace via pady)
        self.index_stats_var = tk.StringVar(master=self.index_popup, value='')
        ttk.Label(frame, textvariable=self.index_stats_var, font=('Arial', 9)).pack(pady=(10, 0))
        self.index_terminal_info = tk.StringVar(master=self.index_popup, value='Check the terminal for more detailed information.')
        ttk.Label(frame, textvariable=self.index_terminal_info, font=('Arial', 9)).pack(pady=(10, 15))
        
        cancel_button = ttk.Button(frame, text='Cancel', command=self.cancel_indexing)
        cancel_button.pack()
        
        self.index_popup.update_idletasks()

        width = self.index_popup.winfo_width()
        height = self.index_popup.winfo_height()
        x = self.winfo_x() + (self.winfo_width() // 2) - (width // 2)
        y = self.winfo_y() + (self.winfo_height() // 2) - (height // 2)
        self.index_popup.geometry(f'{width}x{height}+{x}+{y}')
        
        self.index_popup.protocol('WM_DELETE_WINDOW', self.cancel_indexing)

    def cancel_indexing(self):
        if hasattr(self, '_cancel_event') and self._cancel_event is not None:
            self._cancel_event.set()
        if hasattr(self, 'index_status_var'):
            self.index_status_var.set('Cancelling...')
        if hasattr(self, 'index_popup') and self.index_popup:
            self.index_popup.grab_release()

    def update_gui_progress(self, data):
        if isinstance(data, dict):
            curr = data.get('current', 0)
            total = data.get('total', 1)
            fname = data.get('file', '')
            
            # Calculate percentage for the grey bar
            percent = (curr / total) * 100
            
            # Update the UI on the main thread
            self.after(0, lambda: self.progress_var.set(percent))
            self.after(0, lambda: self.index_filename_var.set(f"File: {fname}"))
            self.after(0, lambda: self.index_stats_var.set(f"Processed {curr} of {total} files"))
            
        else:
            # Fallback for old-style string messages (e.g. "Checking files...")
            self.after(0, lambda: self.index_filename_var.set(str(data)))

    def index_task(self, folder_path: str):
        assert self.db_path is not None
        try:
            result = index_files(
                folder_path, self.device, self.processor, self.model, self.db_path,
                batch_size=self.input_batch_size.get(),
                progress_callback=self.update_gui_progress,
                max_num_patches=self.max_patches_var.get(),
                fast_scene_detect=self.fast_detect_var.get(),
                toggle_preview_callback=self.toggle_preview_playback,
                cancel_event=self._cancel_event
            )
            self.after(0, lambda r=result: self.on_index_finished(r))
        except Exception as e:
            self.after(0, lambda: self.index_popup.destroy() if hasattr(self, 'index_popup') else None)
            self.after(0, lambda: messagebox.showerror('Indexing Error', str(e)))
            print('Indexing Error', str(e))
        finally:
            self.after(0, lambda: self.index_button.config(state='normal'))
            self.after(0, lambda: self.search_button.config(state='normal'))

    def on_index_finished(self, result: str = 'completed'):
        if hasattr(self, 'index_popup') and self.index_popup:
            self.index_popup.destroy()
        if result == 'cancelled':
            self.update_status('Indexing cancelled.')
            messagebox.showinfo('Cancelled', 'Indexing was cancelled.')
        else:
            self.update_status('Indexing complete!')
            messagebox.showinfo('Complete', 'Indexing has finished.')

    def threaded_search(self):
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        assert self.db_path is not None
        # warn if the database contains no entries
        if db_is_empty(self.db_path):
            messagebox.showwarning('Warning', 'The selected database appears to be empty. Please index files before searching.')
            return
        if not self.query_text_var.get() and (not self.query_image_path):
            messagebox.showwarning('Warning', 'Please enter text or select an image to search.')
            return
        self.search_button.config(state='disabled')
        self.update_status('Searching...')
        self.threaded_task(self.search_task)

    def search_task(self):
        assert self.db_path is not None
        try:
            query_embedding = get_query_embedding(self.query_text_var.get(), self.query_image_path, self.device, self.processor, self.model, self.max_patches_var.get())
            if query_embedding is None:
                raise ValueError('Could not generate query embedding.')
            scene_results = search_scenes(query_embedding, self.db_path, top_k=self.top_k_var.get())
            self.after(0, self.on_search_finished, scene_results)
        except Exception as e:
            self.after(0, lambda e=e: messagebox.showerror('Search Error', str(e)))
            print('Search Error', str(e))
        finally:
            self.after(0, lambda: self.search_button.config(state='normal'))

    def on_search_finished(self, results: List[Tuple[str, int, int, int, float]]):
        self.search_results = [(path, score, 'video', None, scene_idx, start_time, end_time) 
                              for path, scene_idx, start_time, end_time, score in results]
        self._update_listview()
        self.update_status(f'Found {len(results)} results.')
        self.rescore_button.config(state='normal' if results else 'disabled')
        self.clear_rescore_button.config(state='disabled')

    def _format_ms(self, ms: int) -> str:
        hours = ms // 3600000
        mins = (ms % 3600000) // 60000
        secs = (ms % 60000) // 1000
        milli = ms % 1000
        
        if hours > 0:
            return f"{hours}:{mins:02d}:{secs:02d}.{milli:03d}"
        return f"{mins}:{secs:02d}.{milli:03d}"

    def _update_listview(self):
        # Clear existing rows
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        if not self.search_results:
            self.stats_label.config(text='No results found.')
            return
        self.last_selected_entry = None
        has_rescore = self.search_results and self.search_results[0][3] is not None
        sort_key = lambda x: x[3] if has_rescore else x[1]
        self.search_results.sort(key=sort_key, reverse=True)
        for i, (path, score, ftype, rescore, scene_idx, scene_time, scene_end) in enumerate(self.search_results, 1):
            filename = os.path.basename(path)
            time_str = ''
            scene_str = ''
            if scene_idx is not None and scene_time is not None:
                # convert milliseconds to M:SS.mmm timecode
                start_str = self._format_ms(scene_time)
                
                if scene_end is not None:
                    end_str = self._format_ms(scene_end)
                    time_str = f'{start_str}-{end_str}'
                else:
                    time_str = start_str
                # Display human-friendly scene number (1-based)
                scene_str = str(scene_idx + 1)
            # Values match columns
            values = [filename, scene_str, time_str, f'{score:.4f}', f'{rescore:.4f}' if rescore is not None else '']
            self.results_tree.insert('', 'end', iid=str(i-1), values=values)
        scores = [rescore if has_rescore and rescore is not None else score for _, score, _, rescore, _, _, _ in self.search_results]
        stats_text = f'Found {len(scores)} results | Max: {max(scores):.3f} | Avg: {np.mean(scores):.3f}'
        self.stats_label.config(text=stats_text)
        # select first row
        first = self.results_tree.get_children()
        if first:
            self.results_tree.selection_set(first[0])
            self.on_result_select(None)

    def open_rescore_dialog(self):
        query_text = simpledialog.askstring('Rescore', 'Enter new text query to rescore results:', parent=self)
        if query_text:
            self.threaded_task(self.rescore_task, query_text)

    def rescore_task(self, query_text: str):
        assert self.db_path is not None
        self.update_status(f"Rescoring with: '{query_text}'...")
        try:
            rescore_embedding = get_query_embedding(query_text, None, self.device, self.processor, self.model)
            if rescore_embedding is None:
                raise ValueError('Could not generate rescore embedding.')
                
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for i, (path, score, ftype, _, scene_idx, scene_time, scene_end) in enumerate(self.search_results):
                    
                    if ftype == 'image':
                        cursor.execute('SELECT embedding FROM image_embeddings WHERE filepath=?', (path,))
                        result = cursor.fetchone()
                        if result:
                            embedding = np.frombuffer(result[0], dtype=np.float32)
                            similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                            self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end)
                            
                    elif ftype == 'video':
                        # Handle merged scenes (tuples) and single scenes (ints)
                        if isinstance(scene_idx, tuple):
                            start_idx, end_idx = scene_idx
                            cursor.execute(
                                'SELECT embedding FROM scene_embeddings WHERE filepath=? AND scene_index >= ? AND scene_index <= ?', 
                                (path, start_idx, end_idx)
                            )
                            results = cursor.fetchall()
                            if results:
                                # Find the best matching scene within the merged block
                                max_sim = -1.0
                                for res in results:
                                    emb = np.frombuffer(res[0], dtype=np.float32)
                                    sim = float(np.dot(emb, rescore_embedding.T).squeeze())
                                    if sim > max_sim:
                                        max_sim = sim
                                self.search_results[i] = (path, score, ftype, max_sim, scene_idx, scene_time, scene_end)
                        else:
                            cursor.execute('SELECT embedding FROM scene_embeddings WHERE filepath=? AND scene_index=?', (path, scene_idx))
                            result = cursor.fetchone()
                            if result:
                                embedding = np.frombuffer(result[0], dtype=np.float32)
                                similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                                self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end)
                                
            self.after(0, self.on_rescore_finished)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('Rescore Error', str(e)))

    def on_rescore_finished(self):
        self._update_listview()
        self.update_status('Rescore complete.')
        self.clear_rescore_button.config(state='normal')

    def clear_rescore(self):
        self.search_results = [(path, score, ftype, None, scene_idx, scene_time, scene_end) for path, score, ftype, _, scene_idx, scene_time, scene_end in self.search_results]
        self._update_listview()
        self.update_status('Rescore cleared.')
        self.clear_rescore_button.config(state='disabled')

    def on_result_select(self, event: Optional[tk.Event]):
        sel = self.results_tree.selection()
        if not sel:
            return
        
        current_selected_entry = sel[0]
        if current_selected_entry == self.last_selected_entry:
            return
        
        self.last_selected_entry = current_selected_entry
        
        index = int(current_selected_entry)
        path, _, file_type, _, scene_idx, scene_time, scene_end = self.search_results[index]
        self.current_display_path = path
        if file_type == 'image':
            self.display_media(path, is_video=False)
        else:
            self.display_media(path, is_video=True, start_ms=scene_time, end_ms=scene_end)

    def on_result_double_click(self, event: tk.Event):
        """Handle double-click on a search result to open the file."""
        iid = self.results_tree.identify_row(event.y)
        if not iid:
            return
            
        # Ensure the row is selected and current_display_path is updated
        self.results_tree.selection_set(iid)
        index = int(iid)
        path = self.search_results[index][0]
        self.current_display_path = path
        
        # Open the file using the system's default application
        self.open_current_file()

    def _render_image_on_canvas(self, use_fast_quality: bool=False):
        if not self.original_image:
            self.image_canvas.delete('all')
            return
        canvas_w = self.image_canvas.winfo_width()
        canvas_h = self.image_canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            self.after(100, self._render_image_on_canvas)
            return
        new_w = int(self.original_image.width * self.canvas_scale)
        new_h = int(self.original_image.height * self.canvas_scale)
        if new_w < 1 or new_h < 1:
            self.image_canvas.delete('all')
            return
        if self.display_image is None or self.display_image.size != (new_w, new_h):
            resample_method = Image.Resampling.BILINEAR if use_fast_quality else Image.Resampling.LANCZOS
            self.display_image = self.original_image.resize((new_w, new_h), resample_method)
        self.tk_image = ImageTk.PhotoImage(self.display_image, master=self)
        self.image_canvas.delete('all')
        draw_x = canvas_w / 2 + self.canvas_offset_x
        draw_y = canvas_h / 2 + self.canvas_offset_y
        self.image_canvas.create_image(draw_x, draw_y, image=self.tk_image)

    def _reset_pan_zoom(self):
        self.canvas_scale = 1.0
        self.canvas_offset_x = 0
        self.canvas_offset_y = 0
        # stop any running video loop when resetting view
        self._stop_video_loop()

    def _show_vlc_player(self, path, start_ms, end_ms):
        media = self.vlc_instance.media_new(path)
        
        # Precise start/stop for scene preview
        if start_ms:
            media.add_option(f'start-time={start_ms / 1000.0}')
        if end_ms:
            media.add_option(f'stop-time={end_ms / 1000.0}')
            
        self.player.set_media(media)
        self.player.play()

    def _show_static_pil_image(self, path):
        # Use your existing high-quality PIL rendering here
        self.original_image = Image.open(path).convert('RGB')
        self._render_image_on_canvas(use_fast_quality=False) # Use LANCZOS for static

    def _on_vlc_end_reached(self, event):
        """Triggered when the video finishes. Bounces to a thread to prevent freezing."""
        self.threaded_task(self._vlc_loop_restart)

    def _vlc_loop_restart(self, media):
        """Restart media using the globally stored current_media reference."""
        if hasattr(self, 'current_media') and self.current_media:
            self.player.set_media(self.current_media)
            self.player.play()

    def _extract_and_show_first_frame(self, path, start_ms):
        """Accurately extracts the frame at the specific timestamp using stream time_base."""
        container = None
        try:
            container = av.open(path)
            stream = container.streams.video[0]
            
            # 1. Convert ms to the stream's internal PTS units
            # formula: pts = (seconds) / time_base
            target_pts = int((start_ms / 1000.0) / float(stream.time_base))
            
            # 2. Seek to the nearest keyframe BEFORE or AT the target
            container.seek(target_pts, stream=stream, any_frame=False, backward=True)
            
            frame_found = False
            for frame in container.decode(stream):
                # 3. Calculate current frame time in ms
                current_frame_ms = int(frame.time * 1000)
                
                # 4. Only accept the frame if it's at or after our target
                if current_frame_ms >= start_ms:
                    img = frame.to_image()
                    self.after(0, self._finalize_frame_update, img)
                    frame_found = True
                    break
            
            if not frame_found:
                # Fallback: if we exhausted the stream, take the last available frame
                pass
                
        except Exception as e:
            self.after(0, lambda: self.update_status(f"Preview Error: {e}"))
        finally:
            if container:
                container.close()

    def _finalize_frame_update(self, pil_img):
        """Main-thread helper to update the image and redraw immediately."""
        self.original_image = pil_img
        self.display_image = None  # Force a re-resize in _render_image_on_canvas
        
        # Reset view and calculate fitting scale
        self.canvas_offset_x = 0
        self.canvas_offset_y = 0
        
        canvas_w = self.image_canvas.winfo_width()
        if canvas_w > 1:
            self.canvas_scale = canvas_w / self.original_image.width
        else:
            self.canvas_scale = 1.0

        # FORCE the actual drawing to happen now
        self._render_image_on_canvas()
        # Optional: Force a GUI update to ensure the canvas refreshes visually
        self.image_canvas.update_idletasks()

    def display_media(self, path: str, is_video: bool, start_ms: int = 0, end_ms: int = 0):
        self._stop_video_loop()
        self.image_canvas.delete("all")
        
        # Reset image state to prevent ghosting
        self.original_image = None
        self.display_image = None
        self.tk_image = None
        
        if not is_video:
            self.player.stop()
            self.video_container.pack_forget() 
            self._show_static_pil_image(path)
            return

        if not config.SCENE_PLAYBACK:
            self.player.stop()
            self.video_container.pack_forget() 
            # Accurate extraction happens here
            self.threaded_task(self._extract_and_show_first_frame, path, start_ms)
        else:
            # Ensure container is visible for VLC
            self.video_container.pack(fill='both', expand=True) 
            self.after(10, lambda: self._start_vlc_playback(path, start_ms, end_ms))

    def _set_vlc_window_handle(self, window_id):
        """Applies the correct window handle based on the platform."""
        if sys.platform == 'win32':
            self.player.set_hwnd(window_id)
        elif sys.platform == 'darwin':
            # macOS uses the NSObject handle for Cocoa windows
            self.player.set_nsobject(window_id)
        else:
            # Linux and other Unix-like systems use X11
            self.player.set_xwindow(window_id)

    def _start_vlc_playback(self, path, start_ms, end_ms):
        try:
            media = self.vlc_instance.media_new(path)
            
            # Subtracting 100ms compensates for VLC's decoding overshoot
            playback_margin_ms = 100

            # Use floating point seconds for VLC options
            if start_ms:
                media.add_option(f'start-time={start_ms / 1000.0}')
            if end_ms:
                # Ensure we don't accidentally set the end time before the start time
                safe_end_ms = max(start_ms + 10, end_ms - playback_margin_ms)
                media.add_option(f'stop-time={safe_end_ms / 1000.0}')

            self.current_media = media 
            self.player.set_media(media)

            # Assign window handle
            h = self.video_container.winfo_id()
            self._set_vlc_window_handle(h)

            self.player.play()
            
            # Setup looping
            events = self.player.event_manager()
            events.event_attach(vlc.EventType.MediaPlayerEndReached, 
                              lambda e: self.threaded_task(self._vlc_loop_restart, media))
        except Exception as e:
            self.update_status(f"VLC Error: {e}")

    def _setup_vlc_loop(self, media):
        # VLC doesn't have a simple 'loop range' flag, so we use events
        events = self.player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerEndReached, 
                        lambda e: self.player.set_media(media) or self.player.play())

    def _stop_video_loop(self):
        if hasattr(self, 'player'):
            self.player.stop()

    def on_player_resize(self, event):
        """Force VLC to re-sync with the window handle on resize."""
        # Added safety check to prevent crash during initialization
        if hasattr(self, 'player') and self.player and self.player.is_playing():
            # Briefly toggle or update the video output
            pass

    def toggle_preview_playback(self):
        config.SCENE_PLAYBACK = not config.SCENE_PLAYBACK
        if hasattr(self, '_playback_toggle_btn'):
            state = 'On' if config.SCENE_PLAYBACK else 'Off'
            self._playback_toggle_btn.config(text=f'Toggle preview playback ({state})')
        # if we just disabled playback, stop any active loop
        if not config.SCENE_PLAYBACK:
            self._stop_video_loop()    
        
        # refresh whatever is currently selected so the preview reflects the new mode
        self.last_selected_entry = None
        self.on_result_select(None)

    def on_canvas_click(self, event: tk.Event):
        self.drag_start_x, self.drag_start_y = (event.x, event.y)

    def on_canvas_drag(self, event: tk.Event):
        # ignore dragging while video is looping (either cached or live)
        if (getattr(self, 'video_cap', None) is not None or getattr(self, 'loop_display_frames', None)) and self.video_loop_id:
            return
        self.canvas_offset_x += event.x - self.drag_start_x
        self.canvas_offset_y += event.y - self.drag_start_y
        self.drag_start_x, self.drag_start_y = (event.x, event.y)
        self._render_image_on_canvas(use_fast_quality=True)

    def on_canvas_zoom(self, event: tk.Event):
        # disable zoom while looping video (cached or live)
        if (getattr(self, 'video_cap', None) is not None or getattr(self, 'loop_display_frames', None)) and self.video_loop_id:
            return
        factor = 1.1 if event.delta > 0 else 1 / 1.1
        self.canvas_scale = max(0.1, min(10.0, self.canvas_scale * factor))
        self._render_image_on_canvas(use_fast_quality=True)
        if self.zoom_timer:
            self.after_cancel(self.zoom_timer)
        self.zoom_timer = self.after(300, lambda: self._render_image_on_canvas(use_fast_quality=False))

    def on_canvas_double_click(self, event: tk.Event):
        if self.current_display_path:
            try:
                if sys.platform == 'win32':
                    os.startfile(self.current_display_path)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', self.current_display_path])
                else:
                    subprocess.run(['xdg-open', self.current_display_path])
            except Exception as e:
                messagebox.showerror('Error', f'Could not open file: {e}')

    def on_canvas_right_click(self, event: tk.Event):
        if not self.current_display_path:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='Copy Path', command=lambda path=self.current_display_path: self.clipboard_append(path))
        menu.add_command(label='Open Containing Folder', command=self.open_containing_folder)
        menu.add_separator()
        menu.add_command(label='Search for Similar', command=self.search_for_similar)
        menu.tk_popup(event.x_root, event.y_root)

    def on_results_right_click(self, event: tk.Event):
        # right-click on a treeview row
        iid = self.results_tree.identify_row(event.y)
        if not iid:
            return
        # select the row under cursor
        self.results_tree.selection_set(iid)
        index = int(iid)
        path = self.search_results[index][0]
        self.current_display_path = path
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='Copy Path', command=lambda p=path: self.clipboard_append(p))
        menu.add_command(label='Open Containing Folder', command=self.open_containing_folder)
        menu.add_command(label='Open File', command=self.open_current_file)
        menu.tk_popup(event.x_root, event.y_root)

    def open_current_file(self):
            """Opens the selected file, using VLC with a timestamp if enabled."""
            if not self.current_display_path:
                return

            # Get the selected item's start time
            sel = self.results_tree.selection()
            start_ms = 0
            if sel:
                index = int(sel[0])
                # search_results index 5 is start_time_ms
                start_ms = self.search_results[index][5] or 0

            # Handle VLC opening logic
            if self.use_vlc_open_var.get():
                start_sec = start_ms / 1000.0


                if sys.platform == 'darwin':
                    vlc_flags = [f":start-time={start_sec}"]
                else:
                    vlc_flags = [f":start-time={start_sec}", "--one-instance", "--no-playlist-enqueue"]

                try:
                    if sys.platform == 'win32':
                        vlc_path = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
                        if os.path.exists(vlc_path):
                            subprocess.Popen([vlc_path, self.current_display_path] + vlc_flags)
                            return
                    elif sys.platform == 'darwin':
                        vlc_path = "/Applications/VLC.app/Contents/MacOS/VLC"
                        if os.path.exists(vlc_path):
                            subprocess.Popen([vlc_path, self.current_display_path] + vlc_flags)
                            return
                    else: # Linux
                        subprocess.Popen(["vlc", self.current_display_path] + vlc_flags)
                        return
                except Exception as e:
                    print(f"Failed to open with VLC: {e}")

            # Fallback to Native Opening if VLC fails or is disabled
            try:
                if sys.platform == 'win32':
                    os.startfile(self.current_display_path)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', self.current_display_path])
                else:
                    subprocess.run(['xdg-open', self.current_display_path])
            except Exception as e:
                messagebox.showerror('Error', f'Could not open file: {e}')

    def open_containing_folder(self):
        if self.current_display_path:
            folder = os.path.dirname(self.current_display_path)
            if sys.platform == 'win32':
                subprocess.run(['explorer', '/select,', self.current_display_path])
            elif sys.platform == 'darwin':
                subprocess.run(['open', '-R', self.current_display_path])
            else:
                subprocess.run(['xdg-open', folder])

    def apply_theme(self):
        """Apply selected theme and save to config."""
        new_theme = self.theme_var.get()
        available_themes = self.style.theme_names()

        if new_theme not in available_themes:
            messagebox.showwarning("Invalid Theme", f"Theme '{new_theme}' not available.")
            return

        try:
            self.style.theme_use(new_theme)
            self.current_theme = new_theme
            self.config['theme'] = new_theme
            config.save_config(self.config)
            self.update_status(f"Theme changed to: {new_theme}")
        except Exception as e:
            messagebox.showerror("Theme Error", f"Failed to apply theme: {e}")

    def search_for_similar(self):
        if self.current_display_path:
            self.query_image_path = self.current_display_path
            self.query_image_var.set(os.path.basename(self.current_display_path))
            self.query_text_var.set('')
            self.threaded_search()

