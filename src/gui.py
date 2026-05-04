import os
import io
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

class ToolTip:
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
from model_loader import get_compute_device
from database import init_db, db_is_empty, cleanup_orphaned_entries, search_scenes
from processing import index_files, get_query_embedding
import config
import gui_utils

def show_splash():
    root = tk.Tk()
    root.withdraw()

    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    
    # Load and resize logo
    img = Image.open(config.text_logo)
    logo_w, logo_h = img.size
    scale = 400 / logo_w
    new_w, new_h = int(logo_w * scale), int(logo_h * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    tk_img = ImageTk.PhotoImage(img, master=splash)
    
    # Image Label
    label = ttk.Label(splash, image=tk_img)
    label.image = tk_img
    label.pack()

    # Attach to splash object so it can be referenced later
    splash.status_label = ttk.Label(
        splash, 
        text="Initializing...", 
        font=("Arial", 10),
        anchor="center"
    )
    splash.status_label.pack(fill='x', pady=10)
    
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
        self.is_active = True
        self.title('Scene Scout')
        self.splash_ref = splash_ref

        self.app_icon = gui_utils.load_app_icon(self, config.big_logo)

        # 1. Load configuration and sync global state
        self.config = config.load_config()
        config.SCENE_PLAYBACK = self.config['scene_playback']

        # 2. INITIALIZE TRACKING VARIABLES IMMEDIATELY
        # This prevents the AttributeError if update_status is called early
        self.status_var = tk.StringVar(master=self, value='Initializing...')
        
        # 3. Setup window geometry and themes
        screen_width = (self.winfo_screenwidth()) - 200
        screen_height = (self.winfo_screenheight()) - 200
        self.geometry(f"{screen_width}x{screen_height}+0+0")
        
        self.style = ThemedStyle(self)
        self.current_theme = self.config['theme']
        self.style.theme_use(self.current_theme)
        self.theme_var = tk.StringVar(master=self, value=self.current_theme)

        # 4. Initialize Config-linked UI Variables
        self.generate_thumbnails_var = tk.BooleanVar(master=self, value=self.config['generate_thumbnails'])
        self.use_trt_var = tk.BooleanVar(master=self, value=self.config['use_trt'])
        self.use_vlc_open_var = tk.BooleanVar(master=self, value=self.config['use_vlc_open'])
        
        # 5. Initialize Hardware/Model Variables
        saved_device = self.config.get('device')
        device_str, device_msg, _, _ = get_compute_device(saved_device)
        self.device_msg = device_msg
        self.device_var = tk.StringVar(master=self, value=device_str)
        
        from model_loader import TRT_AVAILABLE
        self.show_trt_option = (device_str == 'cuda' and TRT_AVAILABLE)

        # 6. Search and index variables
        self.top_k_var = tk.IntVar(master=self, value=self.config['top_k'])
        self.input_batch_size = tk.IntVar(master=self, value=self.config['batch_size'])
        self.fast_detect_var = tk.BooleanVar(master=self, value=self.config['fast_detect'])
        self.max_patches_var = tk.IntVar(master=self, value=self.config['max_patches'])
        self.queue_status_var = tk.StringVar(master=self, value='[0] items in queue')

        # 7. Internal state setup
        self.model = None
        self.processor = None
        self.db_path = None
        self.query_image_path = None
        self.search_results = []
        self.last_selected_entry = None
        
        vlc_args = config.get_vlc_args()
        self.vlc_instance = vlc.Instance(*vlc_args)
        self.player = self.vlc_instance.media_player_new()

        # 8. Run UI construction
        self.setup_widgets()

        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', self.on_handle_drop)

        self.load_saved_paths()
        self.set_controls_enabled(False)
        
        # When closing the app, run the on_closing logic
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.withdraw()


    def setup_widgets(self):
        # Main Layout Containers
        mainframe = ttk.Frame(self, padding='10')
        mainframe.pack(fill='both', expand=True)
        
        main_paned = ttk.PanedWindow(mainframe, orient='horizontal')
        main_paned.pack(fill='both', expand=True)

        # --- LEFT SIDE: Scrollable Controls Pane ---
        controls_pane = ttk.Frame(main_paned)
        main_paned.add(controls_pane, weight=0)

        self.canvas = tk.Canvas(controls_pane, highlightthickness=0, width=320)
        scrollbar = ttk.Scrollbar(controls_pane, orient="vertical", command=self.canvas.yview)
        self.scrollable_controls = ttk.Frame(self.canvas, padding="10")
        
        window_id = self.canvas.create_window((0, 0), window=self.scrollable_controls, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_frame_configure(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _on_canvas_configure(event):
            self.canvas.itemconfig(window_id, width=event.width)

        def _on_mousewheel(event):
            # Prevent scrolling if the event originates from a popup window
            if hasattr(event.widget, 'winfo_toplevel') and event.widget.winfo_toplevel() == self:
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Use Enter/Leave to prevent this from highjacking the whole app
        self.canvas.bind('<Enter>', lambda e: self.canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self.canvas.bind('<Leave>', lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.scrollable_controls.bind("<Configure>", _on_frame_configure)
        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Database Section
        db_frame = ttk.LabelFrame(self.scrollable_controls, text='Database', padding=5)
        db_frame.pack(fill='x', pady=5)
        self.db_var = tk.StringVar(master=self, value='No database selected')
        ttk.Label(db_frame, textvariable=self.db_var, wraplength=250).pack(anchor='w')
        
        db_btn_frame = ttk.Frame(db_frame)
        db_btn_frame.pack(fill='x', pady=2)
        open_db_button = ttk.Button(db_btn_frame, text='Open Existing...', command=self.browse_existing_database)
        open_db_button.pack(side='left', expand=True, fill='x', padx=(0, 2))
        ToolTip(open_db_button, 'Open an existing Scene Scout database (.db) file.')
        create_db_button = ttk.Button(db_btn_frame, text='Create New...', command=self.browse_database)
        create_db_button.pack(side='left', expand=True, fill='x', padx=(2, 0))
        ToolTip(create_db_button, 'Create a new database for indexing media files.')

        # Media Queue Section
        queue_frame = ttk.LabelFrame(self.scrollable_controls, text='Media Queue', padding=5)
        queue_frame.pack(fill='x', pady=5)
        
        # Drag-and-Drop Area
        self.drop_area = ttk.Frame(queue_frame, height=60, relief='solid', borderwidth=2)
        self.drop_area.pack(fill='x', pady=(0, 5))
        self.drop_area.pack_propagate(False)
        drop_label = ttk.Label(self.drop_area, text='Drag & Drop files/folders here\nor click to browse', 
                               anchor='center', justify='center')
        drop_label.pack(expand=True, fill='both')
        ToolTip(self.drop_area, 'Drag and drop files or folders here to add to the queue. Click to browse for files.')
        
        # Bind click and drop events to the drag-and-drop area
        self.drop_area.bind('<Button-1>', lambda e: self.browse_files_dialog())
        self.drop_area.drop_target_register(DND_FILES)
        self.drop_area.dnd_bind('<<Drop>>', self.on_queue_drop)
        
        # Queue Status Label
        ttk.Label(queue_frame, textvariable=self.queue_status_var, font=('', 9, 'bold')).pack(fill='x', pady=(0, 5))
        
        # Buttons Frame
        btn_frame = ttk.Frame(queue_frame)
        btn_frame.pack(fill='x', pady=2)
        
        add_folder_btn = ttk.Button(btn_frame, text='Add Folder(s)', command=self.add_folder_to_queue)
        add_folder_btn.pack(side='left', expand=True, fill='x', padx=(0, 2))
        ToolTip(add_folder_btn, 'Add a directory to the index queue. Recursive by default.')
        
        add_file_btn = ttk.Button(btn_frame, text='Add File(s)', command=self.add_files_to_queue)
        add_file_btn.pack(side='left', expand=True, fill='x', padx=(2, 0))
        ToolTip(add_file_btn, 'Add individual media files to the index queue.')
        
        # Inspect Queue Button
        inspect_btn = ttk.Button(queue_frame, text='Inspect Queue...', command=self.open_queue_manager)
        inspect_btn.pack(fill='x', pady=(5, 3))
        ToolTip(inspect_btn, 'Open the queue manager to view, modify, or remove queued items.')
        
        # Process Button
        self.index_button = ttk.Button(queue_frame, text='Process Media', command=self.threaded_index, state='disabled')
        self.index_button.pack(fill='x', pady=3)
        ToolTip(self.index_button, 'Process all files in the queue and update the scene database.')

        # Search Query Section
        query_frame = ttk.LabelFrame(self.scrollable_controls, text='Search Query', padding=5)
        query_frame.pack(fill='x', pady=5)
        ttk.Label(query_frame, text='Text:').pack(anchor='w')
        self.query_text_var = tk.StringVar(master=self)
        self.query_text_entry = ttk.Entry(query_frame, textvariable=self.query_text_var)
        self.query_text_entry.pack(fill='x', pady=(0, 5))
        ToolTip(self.query_text_entry, 'Enter natural language text to search for matching scenes.')
        self.query_text_entry.bind('<Return>', lambda e: self.threaded_search())
        
        self.query_image_var = tk.StringVar(master=self, value='No query image')
        ttk.Label(query_frame, textvariable=self.query_image_var, wraplength=250).pack(anchor='w')
        
        btn_frame = ttk.Frame(query_frame)
        btn_frame.pack(fill='x', pady=2)
        load_query_button = ttk.Button(btn_frame, text='Load...', command=self.browse_query_image)
        load_query_button.pack(side='left', expand=True, fill='x')
        ToolTip(load_query_button, 'Load an image to use as the search query.')
        clear_query_button = ttk.Button(btn_frame, text='Clear', command=self.clear_query_image)
        clear_query_button.pack(side='left', expand=True, fill='x')
        ToolTip(clear_query_button, 'Clear the current query image from the search form.')
        self.search_button = ttk.Button(query_frame, text='Search Scene', command=self.threaded_search, state='disabled')
        self.search_button.pack(fill='x', pady=3)
        ToolTip(self.search_button, 'Run the search using the current text and/or image query.')

        # Options Section
        options_frame = ttk.LabelFrame(self.scrollable_controls, text='Options', padding=5)
        options_frame.pack(fill='x', pady=5)

        # Compute Device Section
        ttk.Label(options_frame, text='Compute Device:').pack(anchor='w', pady=(5, 0))
        device_options = ['cpu']
        
        # Check for CUDA (NVIDIA/AMD via ROCm)
        if torch.cuda.is_available(): 
            device_options.append('cuda')
            
        # Check for DirectML (Windows AMD/Intel)
        if torch_directml is not None and torch_directml.is_available(): 
            device_options.append('dml')
            
        # Check for Intel Extension for PyTorch (Intel Arc/Integrated XPU)
        if ipex is not None and hasattr(torch, 'xpu') and torch.xpu.is_available():
            device_options.append('xpu')

        # Check for Apple Silicon (MPS)
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device_options.append('mps')

        self.device_combobox = ttk.Combobox(
            options_frame, 
            textvariable=self.device_var, 
            values=device_options, 
            state='readonly'
        )
        self.device_combobox.pack(fill='x')

        # Save config when selection changes
        self.device_combobox.bind("<<ComboboxSelected>>", 
            lambda e: self.save_config_key('device', self.device_var.get()))
        self.device_combobox.pack(fill='x')

        ttk.Label(options_frame, text=f'Auto-detected: {self.device_msg}', font=('', 8, 'italic')).pack(anchor='w')

        # Add TRT Toggle if hardware supports it
        if self.show_trt_option:
            self.trt_check = ttk.Checkbutton(
                options_frame,
                text='Use TensorRT Acceleration', 
                variable=self.use_trt_var,
                command=self.save_trt_preference
            )
            self.trt_check.pack(anchor='w', pady=(5, 0))
        
        ttk.Label(options_frame, text='Detection method:').pack(anchor='w', pady=(5, 0))
        detect_method_frame = ttk.Frame(options_frame)
        detect_method_frame.pack(fill='x', pady=5)
        self.fast_radio = ttk.Radiobutton(detect_method_frame, text='Fast', variable=self.fast_detect_var, 
                value=True, style='Toolbutton',
                command=lambda: self.save_config_key('fast_detect', True))
        self.fast_radio.pack(side='left', expand=True, fill='x', padx=(0, 2))
        ToolTip(self.fast_radio, 'Fast: Use video metadata to extract scenes.')

        self.accurate_radio = ttk.Radiobutton(detect_method_frame, text='Accurate', variable=self.fast_detect_var, 
                        value=False, style='Toolbutton',
                        command=lambda: self.save_config_key('fast_detect', False))
        self.accurate_radio.pack(side='left', expand=True, fill='x', padx=(2, 0))
        ToolTip(self.accurate_radio, 'Accurate: Process video to detect scenes.')

        # Max Patches
        ttk.Label(options_frame, text='Max patches:').pack(anchor='w', pady=(5, 0))
        max_patches_spinbox = ttk.Spinbox(options_frame, from_=128, to=1024, increment=128, 
                    textvariable=self.max_patches_var,
                    command=lambda: self.save_config_key('max_patches', self.max_patches_var.get()))
        max_patches_spinbox.pack(fill='x')
        ToolTip(max_patches_spinbox, 'Number of patches to evaluate per scene; higher values may improve accuracy but increase runtime.')

        # Results (top_k)
        ttk.Label(options_frame, text='Results:').pack(anchor='w', pady=(5, 0))
        top_k_spinbox = ttk.Spinbox(options_frame, from_=1, to=100, 
                    textvariable=self.top_k_var,
                    command=lambda: self.save_config_key('top_k', self.top_k_var.get()))
        top_k_spinbox.pack(fill='x')
        ToolTip(top_k_spinbox, 'How many matching scenes to return for each search.')

        # Batch Size
        ttk.Label(options_frame, text='Scene embed batch size:').pack(anchor='w', pady=(5, 0))
        batch_size_spinbox = ttk.Spinbox(options_frame, from_=8, to=160, 
                    textvariable=self.input_batch_size,
                    command=lambda: self.save_config_key('batch_size', self.input_batch_size.get()))
        batch_size_spinbox.pack(fill='x')
        ToolTip(batch_size_spinbox, 'Number of images processed at once when computing scene embeddings.')

        # VLC Open Preference
        self.vlc_open_check = ttk.Checkbutton(
            options_frame, 
            text='Open video in VLC', 
            variable=self.use_vlc_open_var,
            command=lambda: self.save_config_key('use_vlc_open', self.use_vlc_open_var.get())
        )
        self.vlc_open_check.pack(anchor='w', pady=(5, 0))

        # Thumbnail Toggle
        self.thumb_check = ttk.Checkbutton(
            options_frame, 
            text='Generate Thumbnails (increases DB size)', 
            variable=self.generate_thumbnails_var,
            command=lambda: self.save_config_key('generate_thumbnails', self.generate_thumbnails_var.get())
        )
        self.thumb_check.pack(anchor='w', pady=(5, 0))
        
        # Theme frame
        theme_frame = ttk.Frame(options_frame)
        theme_frame.pack(fill='x', pady=5)
        self.theme_combobox = ttk.Combobox(theme_frame, textvariable=self.theme_var, values=sorted(self.style.theme_names()), state='readonly')
        self.theme_combobox.pack(side='left', fill='x', expand=True)
        apply_theme_button = ttk.Button(theme_frame, text='Apply', command=self.apply_theme)
        apply_theme_button.pack(side='left', padx=(5, 0))
        ToolTip(apply_theme_button, 'Apply the selected GUI theme immediately.')

        # Additional actions Section
        actions_frame = ttk.LabelFrame(self.scrollable_controls, text='Additional Actions', padding=5)
        actions_frame.pack(fill='x', pady=10)
        
        self.load_model_button = ttk.Button(actions_frame, text='Load Model', command=self.threaded_load_model)
        self.load_model_button.pack(fill='x', pady=3)
        ToolTip(self.load_model_button, 'Load or reload the model used for scene search.')
        
        cleanup_button = ttk.Button(actions_frame, text='Cleanup Database', command=self.cleanup_database)
        cleanup_button.pack(fill='x', pady=3)
        ToolTip(cleanup_button, 'Remove orphaned or invalid database entries.')

        # Info Section
        info_frame = ttk.LabelFrame(self.scrollable_controls, text='Info', padding=5)
        info_frame.pack(fill='x', pady=(0,10))
        about_button = ttk.Button(info_frame, text='About', command=self.open_about_dialog)
        about_button.pack(fill='x')
        ToolTip(about_button, 'Open the about dialog with project and version information.')

        # Status Label
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
        ToolTip(self.rescore_button, 'Open rescore dialog to adjust scene result rankings.')
        self.clear_rescore_button = ttk.Button(rescore_frame, text='Clear Rescore', command=self.clear_rescore, state='disabled')
        self.clear_rescore_button.pack(side='left', padx=5)
        ToolTip(self.clear_rescore_button, 'Clear any custom rescore adjustments from results.')
        
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
        ToolTip(self._playback_toggle_btn, 'Toggle video preview playback on or off for selected results.')
        
        self.preview_image_canvas = tk.Canvas(preview_frame, bg='gray')
        self.preview_image_canvas.pack(fill='both', expand=True)

        self.preview_image_canvas.bind('<Button-1>', self.on_canvas_click)
        self.preview_image_canvas.bind('<B1-Motion>', self.on_canvas_drag)
        self.preview_image_canvas.bind('<MouseWheel>', self.on_canvas_zoom)
        self.preview_image_canvas.bind('<Double-1>', self.on_canvas_double_click)
        self.preview_image_canvas.bind('<Button-3>', self.on_canvas_right_click)
        
        self.video_container = ttk.Frame(self.preview_image_canvas)
        self.video_container.pack(fill='both', expand=True)

        self.thumb_outer_frame = ttk.Frame(preview_frame, height=330) 
        self.thumb_outer_frame.pack(fill='x', side='bottom', pady=(5,0))
        self.thumb_outer_frame.pack_propagate(False)

        # Scrollable canvas for the vertical strip
        self.thumb_canvas = tk.Canvas(self.thumb_outer_frame, height=300, highlightthickness=0)
        self.thumb_scrollbar = ttk.Scrollbar(self.thumb_outer_frame, orient='horizontal', command=self.thumb_canvas.xview)
        
        self.thumb_canvas.configure(xscrollcommand=self.thumb_scrollbar.set)

        self.thumb_inner_frame = ttk.Frame(self.thumb_canvas)
        self.thumb_canvas.create_window((0, 0), window=self.thumb_inner_frame, anchor='nw')

        # Auto-update the scroll region when thumbnails are added
        self.thumb_inner_frame.bind('<Configure>', lambda e: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox('all')))
        
        self.thumb_canvas.pack(side='top', fill='x', expand=True)
        self.thumb_scrollbar.pack(side='bottom', fill='x')

        def _on_thumb_mousewheel(event):
            # Scrolls horizontally since the thumbnail bar is horizontal
            # Prevent scrolling if the event originates from a popup window
            if hasattr(event.widget, 'winfo_toplevel') and event.widget.winfo_toplevel() == self:
                self.thumb_canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

        self.thumb_canvas.bind('<Enter>', lambda e: self.thumb_canvas.bind_all("<MouseWheel>", _on_thumb_mousewheel))
        self.thumb_canvas.bind('<Leave>', lambda e: self.thumb_canvas.unbind_all("<MouseWheel>"))
        
        # Persistent storage to prevent Python's garbage collector from deleting the images
        self.thumbnail_references = []
        self.thumbnail_widgets = {}

        paned_window.add(preview_frame, weight=1)

    def _on_closing(self):
        """Cleanup resources before destroying the window."""
        self._stop_video_loop()
        self.destroy()

    def on_handle_drop(self, event):
        # tkinterdnd2 returns paths in a specific format (spaces are handled with braces)
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        
        # Process .db files and query images first (first such file found)
        for path in paths:
            if path.lower().endswith('.db'):
                self.db_path = path
                self.db_var.set(os.path.basename(path))
                init_db(self.db_path)
                self.config['db_path'] = path
                config.save_config(self.config)
                self.update_status(f"Database loaded via drop: {os.path.basename(path)}")
                self.update_queue_status()
                break
        
        for path in paths:
            if path.lower().endswith(config.IMAGE_EXTENSIONS):
                self.query_image_path = path
                self.query_image_var.set(os.path.basename(path))
                self.update_status(f"Query image set via drop: {os.path.basename(path)}")
                break
        
        # Add all valid media files and directories to queue
        media_paths = [p for p in paths if not p.lower().endswith('.db') and 
                        (os.path.isdir(p) or p.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS))]
        if media_paths:
            self._add_paths_to_queue(media_paths)

    def threaded_task(self, target_func: Callable, *args):
        # Only spawn a thread if GUI is actively running
        if getattr(self, 'is_active', True):
            thread = threading.Thread(target=target_func, args=args, daemon=True)
            thread.start()

    def set_controls_enabled(self, enabled: bool):
        state = 'normal' if enabled else 'disabled'
        for name in ['load_model_button', 'index_button', 'search_button', 'rescore_button', 'clear_rescore_button', 'query_text_entry']:
            widget = getattr(self, name, None)
            if widget:
                try:
                    widget.config(state=state)
                except Exception:
                    pass
    
    def save_config_key(self, key, value):
        """Updates internal config and persists to disk."""
        self.config[key] = value
        config.save_config(self.config)

    def save_trt_preference(self):
        """Save the TensorRT preference to the GUI config file."""
        self.config['use_trt'] = self.use_trt_var.get()
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
            
            # Migrate old folder_path to index_queue if present
            if 'folder_path' in self.config and self.config['folder_path'] and os.path.exists(self.config['folder_path']):
                from database import add_to_queue, queue_count
                if queue_count(self.db_path) == 0:
                    add_to_queue(self.db_path, self.config['folder_path'], is_directory=True, recursive=True)
                # Remove old folder_path from config
                del self.config['folder_path']
                config.save_config(self.config)
        
        self.update_queue_status()

    def update_queue_status(self):
        """Update the queue status label and process button state."""
        if not self.db_path:
            self.queue_status_var.set('[0] items in queue (no database)')
            self.index_button.config(state='disabled')
            return
        from database import queue_count
        count = queue_count(self.db_path)
        self.queue_status_var.set(f'[{count}] items in queue')
        self.index_button.config(state='normal' if count > 0 else 'disabled')

    def on_queue_drop(self, event):
        """Handle drops on the dedicated drag-and-drop area."""
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        self._add_paths_to_queue(paths)

    def _add_paths_to_queue(self, paths):
        """Validate and add multiple paths to the index queue."""
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        from database import add_to_queue, queue_count
        added = 0
        for path in paths:
            if os.path.exists(path) and not path.lower().endswith('.db'):
                is_dir = os.path.isdir(path)
                if not is_dir and not path.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS):
                    continue
                add_to_queue(self.db_path, path, is_directory=is_dir, recursive=is_dir)
                added += 1
        if added > 0:
            self.update_queue_status()
            self.update_status(f'Added {added} item(s) to queue.')
        elif paths:
            self.update_status('No valid media files or directories dropped.')

    def browse_files_dialog(self):
        """Open file browser when clicking the drag-and-drop area."""
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        path = filedialog.askopenfilename(
            title='Select Media Files',
            filetypes=[('Media Files', ' '.join(f'*{ext}' for ext in config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS))],
            multiple=True
        )
        if path:
            self._add_paths_to_queue(path)

    def add_folder_to_queue(self):
        """Add a directory to the index queue."""
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        path = filedialog.askdirectory(title='Select Folder to Add to Queue')
        if path:
            self._add_paths_to_queue([path])

    def add_files_to_queue(self):
        """Add individual files to the index queue."""
        self.browse_files_dialog()

    def open_queue_manager(self):
        """Open a popup to inspect and manage the index queue."""
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return

        from database import get_queue, remove_from_queue, clear_queue, update_queue_recursive, queue_count

        dlg = tk.Toplevel(self)
        gui_utils.apply_window_icon(dlg, self.app_icon)
        dlg.title('Queue Manager')
        dlg.transient(self)
        dlg.grab_set()
        dlg.minsize(700, 500)
        
        # Center window
        gui_utils.center_window(dlg, 700, 500)

        main_frame = ttk.Frame(dlg, padding=10)
        main_frame.pack(fill='both', expand=True)

        # Treeview with scrollbar
        tree_frame = ttk.Frame(main_frame)
        tree_frame.pack(fill='both', expand=True, pady=(0, 10))

        columns = ('type', 'name', 'path', 'recursive')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings', 
                           selectmode='extended', height=15)
        tree.heading('type', text='Type')
        tree.heading('name', text='Name')
        tree.heading('path', text='Full Path')
        tree.heading('recursive', text='Recursive')
    
        tree.column('type', width=60, anchor='center')
        tree.column('name', width=150, anchor='w')
        tree.column('path', width=350, anchor='w')
        tree.column('recursive', width=70, anchor='center')

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Store mapping from tree item id to queue id
        item_to_queue_id = {}

        def refresh_tree():
            for item in tree.get_children():
                tree.delete(item)
            item_to_queue_id.clear()
            queue_items = get_queue(self.db_path)
            missing_count = 0
            for qid, path, is_directory, recursive in queue_items:
                item_type = 'Folder' if is_directory else 'File'
                name = os.path.basename(path) or path
                if is_directory:
                    rec_text = 'Yes' if recursive else 'No'
                else:
                    rec_text = '-'
                exists = os.path.exists(path)
                if not exists:
                    name = f'[MISSING] {name}'
                    missing_count += 1
                iid = tree.insert('', 'end', values=(item_type, name, path, rec_text),
                                  tags=('missing' if not exists else ''))
                item_to_queue_id[iid] = qid
            tree.tag_configure('missing', foreground='gray')
            update_status_label(missing_count)

        def update_status_label(missing=0):
            count = queue_count(self.db_path)
            text = f'{count} item(s) in queue'
            if missing > 0:
                text += f' ({missing} missing)'
            status_var.set(text)

        # Status label
        status_var = tk.StringVar(master=self)
        ttk.Label(main_frame, textvariable=status_var, font=('Arial', 9, 'bold')).pack(anchor='w', pady=(0, 5))

        # Buttons frame
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill='x', pady=5)

        def remove_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning('Warning', 'No items selected.')
                return
            if messagebox.askyesno('Confirm', 'Remove %d selected item(s)?' % len(selected)):
                for iid in selected:
                    qid = item_to_queue_id.get(iid)
                    if qid:
                        remove_from_queue(self.db_path, qid)
                refresh_tree()
                self.update_queue_status()

        def clear_all():
            if messagebox.askyesno('Confirm', 'Clear all items from the queue?'):
                clear_queue(self.db_path)
                refresh_tree()
                self.update_queue_status()

        def toggle_recursive_for_selected(recursive_val):
            selected = tree.selection()
            if not selected:
                return
            for iid in selected:
                qid = item_to_queue_id.get(iid)
                if qid:
                    values = tree.item(iid)['values']
                    if values and values[0] == 'Folder':
                        update_queue_recursive(self.db_path, qid, recursive_val)
            refresh_tree()

        def clean_missing():
            selected = [iid for iid in tree.get_children()
                        if '[MISSING]' in str(tree.item(iid)['values'][1])]
            if not selected:
                messagebox.showinfo('Info', 'No missing items in queue.')
                return
            if messagebox.askyesno('Confirm', 'Remove %d missing item(s)?' % len(selected)):
                for iid in selected:
                    qid = item_to_queue_id.get(iid)
                    if qid:
                        remove_from_queue(self.db_path, qid)
                refresh_tree()
                self.update_queue_status()

        ttk.Button(btn_frame, text='Remove Selected', command=remove_selected).pack(side='left', padx=(0, 5))
        ttk.Button(btn_frame, text='Clear Queue', command=clear_all).pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Clean Missing', command=clean_missing).pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Set Recursive ON',
                   command=lambda: toggle_recursive_for_selected(True)).pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Set Recursive OFF',
                   command=lambda: toggle_recursive_for_selected(False)).pack(side='left', padx=5)

        # Right-click context menu
        def show_context_menu(event):
            item = tree.identify_row(event.y)
            if not item:
                return
            menu = tk.Menu(tree, tearoff=0)
            selected = tree.selection()
            if item not in selected:
                tree.selection_set(item)
                selected = [item]
        
            menu.add_command(label='Remove Selected', command=remove_selected)
        
            has_folder = any(tree.item(i)['values'][0] == 'Folder' for i in selected)
            if has_folder:
                menu.add_separator()
                menu.add_command(label='Set Recursive ON', 
                             command=lambda: toggle_recursive_for_selected(True))
                menu.add_command(label='Set Recursive OFF', 
                             command=lambda: toggle_recursive_for_selected(False))
        
            menu.post(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        # Keyboard shortcut for delete
        def on_key_press(event):
            if event.keysym in ('Delete', 'BackSpace'):
                remove_selected()

        dlg.bind('<KeyPress>', on_key_press)

        # Inline recursive toggle on click (simplified - just use column detection)
        def on_tree_click(event):
            item = tree.identify_row(event.y)
            column = tree.identify_column(event.x)
            if item and column == '#4':  # Recursive column
                qid = item_to_queue_id.get(item)
                values = tree.item(item)['values']
                if qid and values and values[0] == 'Folder':
                    new_rec = not (values[3] == 'Yes')
                    update_queue_recursive(self.db_path, qid, new_rec)
                    refresh_tree()

        tree.bind('<ButtonRelease-1>', on_tree_click)

        # Initial load
        refresh_tree()

        # Close button
        ttk.Button(main_frame, text='Close', command=dlg.destroy).pack(pady=(10, 0))

        dlg.protocol('WM_DELETE_WINDOW', dlg.destroy)

    def browse_database(self):
        path = filedialog.asksaveasfilename(title='Create New Database File', initialdir=self.config.get('db_path', ''), filetypes=[('SQLite Database', '*.db')], defaultextension='.db')
        if path:
            self.db_path = path
            self.db_var.set(os.path.basename(path))
            init_db(self.db_path)
            self.config['db_path'] = path
            config.save_config(self.config)
            self.update_status(f'Database set to: {os.path.basename(path)}')
            self.update_queue_status()

    def browse_existing_database(self):
        path = filedialog.askopenfilename(title='Select Existing Database File', initialdir=self.config.get('db_path', ''), filetypes=[('SQLite Database', '*.db')])
        if path:
            self.db_path = path
            self.db_var.set(os.path.basename(path))
            init_db(self.db_path)
            self.config['db_path'] = path
            config.save_config(self.config)
            self.update_status(f'Database set to: {os.path.basename(path)}')
            self.update_queue_status()

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
        gui_utils.apply_window_icon(dlg, self.app_icon)
        gui_utils.center_window(dlg, 450, 380)
        dlg.title('About Scene Scout')
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.grab_set()

        main_frame = ttk.Frame(dlg, padding=20)
        main_frame.pack(fill='both', expand=True)

        # --- Header: Icon + Title + Version ---
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill='x', pady=(0, 15))

        # Resize app icon for dialog
        icon_size = 48
        icon_photo = None
        if self.app_icon and hasattr(self.app_icon, '_PhotoImage__photo'):
            original_img = Image.open(config.big_logo).resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            icon_photo = ImageTk.PhotoImage(original_img, master=dlg)
        elif self.app_icon:
            original_img = Image.open(config.big_logo).resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            icon_photo = ImageTk.PhotoImage(original_img, master=dlg)

        if icon_photo:
            icon_label = ttk.Label(header_frame, image=icon_photo)
            icon_label.image = icon_photo
            icon_label.pack(side='left', padx=(0, 12))

        title_frame = ttk.Frame(header_frame)
        title_frame.pack(side='left', fill='both', expand=True)

        title_label = ttk.Label(title_frame, text='Scene Scout', font=('', 14, 'bold'))
        title_label.pack(anchor='w')

        # Load version from pyproject.toml
        version_text = ''
        try:
            import toml
            pyproject_path = config.PROJECT_ROOT / 'pyproject.toml'
            if pyproject_path.exists():
                with open(pyproject_path, 'r') as f:
                    pyproject = toml.load(f)
                ver = pyproject.get('project', {}).get('version', '')
                if ver:
                    version_text = f'v{ver}'
        except Exception:
            pass

        if version_text:
            version_label = ttk.Label(title_frame, text=version_text, font=('', 10))
            version_label.pack(anchor='w')

        # --- Description Card ---
        desc_frame = ttk.LabelFrame(main_frame, text='About', padding=12)
        desc_frame.pack(fill='both', expand=True, pady=(0, 15))

        desc_text = (
            "Scene Scout is a tool written to help with searching for "
            "specific scenes using keywords. It is forked and built on "
            "top of Gabrjiele's project and uses Google's SigLIP 2 model "
            "for embedding and extracting visual information."
        )
        desc_label = ttk.Label(desc_frame, text=desc_text, wraplength=380, justify='left')
        desc_label.pack(anchor='w')

        # --- Link Pills ---
        link_frame = ttk.Frame(main_frame)
        link_frame.pack(fill='x', pady=(0, 15))

        def make_pill(parent, text, url):
            btn = ttk.Button(parent, text=text, command=lambda: webbrowser.open_new(url))
            btn.pack(side='left', padx=3)

        make_pill(link_frame, 'Original Source', 'https://github.com/Gabrjiele/siglip2-naflex-search')
        make_pill(link_frame, 'Logo by Miwo', 'https://4miwo.carrd.co')
        make_pill(link_frame, 'GitHub Repo', 'https://github.com/Mark-Shun/scene-scout')
        make_pill(link_frame, 'Codeberg Repo', 'https://codeberg.org/Mark-Shun/scene-scout')
        make_pill(link_frame, 'Gitlab Repo', 'https://gitlab.com/Mark-Shun/scene-scout')

        # --- Close Button ---
        close_btn = ttk.Button(main_frame, text='Close', command=dlg.destroy)
        close_btn.pack(pady=(5, 0))

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
        if not self.db_path:
            messagebox.showerror('Error', 'Please select a database first.')
            return
        from database import queue_count
        if queue_count(self.db_path) == 0:
            messagebox.showerror('Error', 'Please add files or folders to the queue before indexing.')
            return
        self.index_button.config(state='disabled')
        self.search_button.config(state='disabled')
        self._cancel_event = threading.Event()
        self._stop_video_loop()
        self.show_indexing_popup()
        self.update_status('Indexing in progress...')
        self.threaded_task(self.index_task)

    def show_indexing_popup(self):
        self.index_popup = tk.Toplevel(self)
        gui_utils.apply_window_icon(self.index_popup, self.app_icon)
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
        if hasattr(self, 'index_filename_var'):
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

    def index_task(self):
        assert self.db_path is not None
        try:
            result = index_files(
                self.device, self.processor, self.model, self.db_path,
                batch_size=self.input_batch_size.get(),
                generate_thumbnails=self.generate_thumbnails_var.get(),
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
            if self.db_path:
                from database import clear_queue
                clear_queue(self.db_path)
                self.update_queue_status()
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
        self._stop_video_loop()
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

    def on_search_finished(self, results: List[Tuple[str, int, int, int, bytes, float]]):
        self.search_results = [(path, score, 'video', None, scene_idx, start_time, end_time, thumb_bytes) 
                              for path, scene_idx, start_time, end_time, thumb_bytes, score in results]
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
        # 1. Clear existing rows and thumbnails
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)

        for widget in self.thumb_inner_frame.winfo_children():
            widget.destroy()

        self.thumbnail_references.clear()
        self.thumbnail_widgets = {} 
        visible_thumb_count = 0

        if not self.search_results:
            self.stats_label.config(text='No results found.')
            return
        
        self.last_selected_entry = None
        has_rescore = self.search_results and self.search_results[0][3] is not None
        sort_key = lambda x: x[3] if has_rescore else x[1]
        self.search_results.sort(key=sort_key, reverse=True)

        # 2. Populate the Treeview and Thumbnail Strip
        for i, data in enumerate(self.search_results, 1):
            path, score, ftype, rescore, scene_idx, scene_time, scene_end, thumb_bytes = data
            tree_id = str(i-1)
            
            filename = os.path.basename(path)
            time_str = ''
            scene_str = ''

            if scene_idx is not None and scene_time is not None:
                start_str = self._format_ms(scene_time)
                if scene_end is not None:
                    end_str = self._format_ms(scene_end)
                    time_str = f'{start_str}-{end_str}'
                else:
                    time_str = start_str
                scene_str = str(scene_idx + 1)
            
            values = [filename, scene_str, time_str, f'{score:.4f}', f'{rescore:.4f}' if rescore is not None else '']
            self.results_tree.insert('', 'end', iid=tree_id, values=values)

            if thumb_bytes:
                img_data = io.BytesIO(thumb_bytes)
                img = Image.open(img_data)
                tk_img = ImageTk.PhotoImage(img, master=self)
                self.thumbnail_references.append(tk_img)
                
                thumb_container = tk.Frame(self.thumb_inner_frame, bd=2, relief='flat')
                row_idx = visible_thumb_count % 3       
                col_idx = visible_thumb_count // 3      
                thumb_container.grid(row=row_idx, column=col_idx, padx=2, pady=2)
                
                thumb_lbl = ttk.Label(thumb_container, image=tk_img, cursor='hand2')
                thumb_lbl.pack()
                thumb_lbl.bind('<Button-1>', lambda e, iid=tree_id: self.on_thumbnail_click(iid))
                
                self.thumbnail_widgets[tree_id] = thumb_container
                visible_thumb_count += 1

        # 3. Update status and auto-select first result
        scores = [rescore if has_rescore and rescore is not None else score for _, score, _, rescore, _, _, _, _ in self.search_results]
        stats_text = f'Found {len(scores)} results | Max: {max(scores):.3f} | Avg: {np.mean(scores):.3f}'
        self.stats_label.config(text=stats_text)
        
        first = self.results_tree.get_children()
        if first:
            self.results_tree.selection_set(first[0])
            self.on_result_select(None)

    def on_thumbnail_click(self, tree_iid: str):
        """Triggered when a thumbnail is clicked. Selects the corresponding row in the treeview."""
        self.results_tree.selection_set(tree_iid)
        self.results_tree.see(tree_iid) # Scroll treeview to the item
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
                for i, (path, score, ftype, _, scene_idx, scene_time, scene_end, thumb_bytes) in enumerate(self.search_results):
                    
                    if ftype == 'image':
                        cursor.execute('SELECT embedding FROM image_embeddings WHERE filepath=?', (path,))
                        result = cursor.fetchone()
                        if result:
                            embedding = np.frombuffer(result[0], dtype=np.float32)
                            similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                            self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end, thumb_bytes)
                            
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
                                self.search_results[i] = (path, score, ftype, max_sim, scene_idx, scene_time, scene_end, thumb_bytes)
                        else:
                            cursor.execute('SELECT embedding FROM scene_embeddings WHERE filepath=? AND scene_index=?', (path, scene_idx))
                            result = cursor.fetchone()
                            if result:
                                embedding = np.frombuffer(result[0], dtype=np.float32)
                                similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                                self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end, thumb_bytes)
                                
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
            
            # Retrieve the unique ID string for the row (e.g., '0', '1', '2')
            current_selected_entry = sel[0]
            if current_selected_entry == self.last_selected_entry:
                return
            
            self.last_selected_entry = current_selected_entry
            index = int(current_selected_entry)
            
            # Extract metadata from search_results (matches the 8-item tuple structure)
            # Structure: path, score, ftype, rescore, scene_idx, start_ms, end_ms, thumb_bytes
            path, _, file_type, _, _, scene_time, scene_end, _ = self.search_results[index]
            self.current_display_path = path
            
            # --- THUMBNAIL HIGHLIGHTING LOGIC ---
            # Reset all existing thumbnail borders using dictionary values
            for widget in self.thumbnail_widgets.values():
                widget.config(relief='flat', bg=self.cget('bg'))
                
            # Highlight ONLY if this result actually has a visual thumbnail widget mapped to its ID
            if current_selected_entry in self.thumbnail_widgets:
                selected_widget = self.thumbnail_widgets[current_selected_entry]
                selected_widget.config(relief='solid', bg='#0078D7') 
                
                # Update geometry to ensure scrolling coordinates are fresh
                self.update_idletasks()
                
                # Automatically scroll the horizontal thumbnail canvas to the widget
                x_pos = selected_widget.winfo_x()
                canvas_width = self.thumb_canvas.winfo_width()
                inner_width = self.thumb_inner_frame.winfo_width()
                
                if inner_width > canvas_width:
                    # Calculate the fraction needed to bring the thumbnail into view
                    scroll_fraction = max(0, (x_pos - (canvas_width / 2)) / inner_width)
                    self.thumb_canvas.xview_moveto(scroll_fraction)
            
            # --- MEDIA DISPLAY LOGIC ---
            if file_type == 'image':
                self.display_media(path, is_video=False)
            else:
                # scene_time and scene_end are in milliseconds
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

    def _render_preview_image_on_canvas(self):
        if not self.original_image:
            self.preview_image_canvas.delete('all')
            return

        canvas_w = self.preview_image_canvas.winfo_width()
        canvas_h = self.preview_image_canvas.winfo_height()

        if canvas_w <= 1 or canvas_h <= 1:
            self.after(100, self._render_preview_image_on_canvas)
            return

        new_w = int(self.original_image.width * self.canvas_scale)
        new_h = int(self.original_image.height * self.canvas_scale)

        if new_w < 1 or new_h < 1:
            self.preview_image_canvas.delete('all')
            return

        # Always use high-quality resampling
        self.display_image = self.original_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(self.display_image, master=self)
        
        self.preview_image_canvas.delete('all')
        draw_x = canvas_w / 2 + self.canvas_offset_x
        draw_y = canvas_h / 2 + self.canvas_offset_y
        self.preview_image_canvas.create_image(draw_x, draw_y, image=self.tk_image)

    def _show_static_pil_image(self, path):
        # Use your existing high-quality PIL rendering here
        self.original_image = Image.open(path).convert('RGB')
        self._render_preview_image_on_canvas() # Use LANCZOS for static

    def _vlc_loop_restart(self):
        """Restarts the currently assigned media to create a loop effect."""
        if hasattr(self, 'current_media') and self.current_media:
            # Re-setting the media is required in VLC to respect start/stop-time options
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
        self.display_image = None  # Force a re-resize in _render_preview_image_on_canvas
        
        # Reset view and calculate fitting scale
        self.canvas_offset_x = 0
        self.canvas_offset_y = 0
        
        canvas_w = self.preview_image_canvas.winfo_width()
        if canvas_w > 1:
            self.canvas_scale = canvas_w / self.original_image.width
        else:
            self.canvas_scale = 1.0

        # FORCE the actual drawing to happen now
        self._render_preview_image_on_canvas()
        # Optional: Force a GUI update to ensure the canvas refreshes visually
        self.preview_image_canvas.update_idletasks()

    def display_media(self, path: str, is_video: bool, start_ms: int = 0, end_ms: int = 0):
        self._stop_video_loop()
        self.preview_image_canvas.delete("all")
        
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
                safe_end_ms = max(start_ms + 50, end_ms - playback_margin_ms)
                media.add_option(f'stop-time={safe_end_ms / 1000.0}')

            self.current_media = media 
            self.player.set_media(media)
            
            # Set up looping using a clean thread bounce
            events = self.player.event_manager()
            events.event_attach(vlc.EventType.MediaPlayerEndReached, 
                            lambda e: self.threaded_task(self._vlc_loop_restart))

            # Assign window handle
            h = self.video_container.winfo_id()
            self._set_vlc_window_handle(h)

            self.player.play()
            
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
            # Force macOS to release the hardware/GPU lock
            if sys.platform == 'darwin':
                self.player.set_media(None)

    def toggle_preview_playback(self):
        # Toggle the global state
        config.SCENE_PLAYBACK = not config.SCENE_PLAYBACK
        
        # Update the internal config dictionary
        self.config['scene_playback'] = config.SCENE_PLAYBACK
        
        # Persist the change to the JSON file
        config.save_config(self.config)
        
        # Refresh UI elements
        state = 'On' if config.SCENE_PLAYBACK else 'Off'
        self._playback_toggle_btn.config(text=f'Toggle preview playback ({state})')
        
        if not config.SCENE_PLAYBACK:
            self._stop_video_loop()    
        
        self.last_selected_entry = None
        self.on_result_select(None)

    def on_canvas_click(self, event: tk.Event):
        self.drag_start_x, self.drag_start_y = (event.x, event.y)

    def on_canvas_drag(self, event: tk.Event):
        self.canvas_offset_x += event.x - self.drag_start_x
        self.canvas_offset_y += event.y - self.drag_start_y
        self.drag_start_x, self.drag_start_y = (event.x, event.y)
        self._render_preview_image_on_canvas()

    def on_canvas_zoom(self, event: tk.Event):
        factor = 1.1 if event.delta > 0 else 1 / 1.1
        self.canvas_scale = max(0.1, min(10.0, self.canvas_scale * factor))
        self._render_preview_image_on_canvas()

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
        menu.add_command(label='Search for Similar', command=self.search_for_similar_preview_frame)
        menu.post(event.x_root, event.y_root)

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
        menu.post(event.x_root, event.y_root)

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
        new_theme = self.theme_var.get()
        try:
            self.style.theme_use(new_theme)
            
            # Manually grab the background color from the new theme
            bg_color = self.style.lookup('TFrame', 'background')
            
            # Update the standard tk widgets that don't auto-theme
            self.preview_image_canvas.config(bg=bg_color)
            self.canvas.config(bg=bg_color) 
            self.thumb_canvas.config(bg=bg_color)            

            self.config['theme'] = new_theme
            config.save_config(self.config)
        except Exception as e:
            messagebox.showerror("Theme Error", f"Failed to apply theme: {e}")

    def search_for_similar_preview_frame(self):
        if not self.current_display_path:
                    return

        # If we have a frame in memory (Image or extracted Video frame)
        if self.original_image:
            os.makedirs(config.TEMP_FOLDER, exist_ok=True)
            # Store image in temporary folder
            temp_filename = "temp_search_query.jpg"
            temp_path = os.path.abspath(os.path.join(config.TEMP_FOLDER, temp_filename))

            try:
                # Save the high-quality original frame
                self.original_image.save(temp_path, "JPEG", quality=95)
                
                # Set this temp file as the query image
                self.query_image_path = temp_path
                self.query_image_var.set(f"Frame from {os.path.basename(self.current_display_path)}")
                self.query_text_var.set('')
                
                # Run the search
                self.threaded_search()
            except Exception as e:
                messagebox.showerror("Search Error", f"Could not capture frame: {e}")
        else:
            # Fallback: if it's a standard image file and original_image isn't set
            if self.current_display_path.lower().endswith(config.IMAGE_EXTENSIONS):
                self.query_image_path = self.current_display_path
                self.query_image_var.set(os.path.basename(self.current_display_path))
                self.query_text_var.set('')
                self.threaded_search()
