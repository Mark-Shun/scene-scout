import os
import io
import sys
import threading
import tkinter as tk
import subprocess
import webbrowser
import sqlite3
import gc
from model_loader import load_siglip_model
from tkinter import filedialog, messagebox, ttk, simpledialog
from typing import Callable, List, Optional, Tuple
from ttkthemes import ThemedStyle
from tkinterdnd2 import DND_FILES, TkinterDnD
from pathlib import Path

import av
import numpy as np
import torch

from gui_utils import ToolTip

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
    splash = tk.Tk()
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
    splash.status_label = ttk.Label(splash, text="Initializing...", font=("Arial", 10), anchor="center")
    splash.status_label.pack(fill='x', pady=10)
    
    # Calculate center position including the new label height
    splash.update_idletasks()
    w, h = splash.winfo_reqwidth(), splash.winfo_reqheight()
    sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    splash.geometry(f"{w}x{h}+{x}+{y}")
    
    splash.lift()
    return splash

class SceneScoutApp(TkinterDnD.Tk):

    def __init__(self, splash_ref=None):
        super().__init__()
        self.withdraw()
        self.is_active = True
        self.title('Scene Scout')
        self.splash_ref = splash_ref

        self.app_icon = gui_utils.load_app_icon(self, config.big_logo)

        # 1. Load configuration and sync global state
        self.config = config.load_config()
        config.SCENE_PLAYBACK = self.config['scene_playback']

        # GPU offload
        self._is_in_standby = False
        self.gpu_standby_var = tk.BooleanVar(master=self, value=self.config.get('gpu_standby', True))
        self._idle_counter = 0
        self._idle_after_id = None

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
        self.active_databases: List[str] = []
        self.primary_db: Optional[str] = None
        self.db_manager_dlg = None
        self.queue_manager_dlg = None
        self.query_image_path = None
        self.search_results = []
        self.last_selected_entry = None
        self.current_sort_col = 'score'
        self.current_sort_reverse = True
        self.canvas_scale = 1.0
        self.canvas_offset_x = 0
        self.canvas_offset_y = 0

        vlc_args = config.get_vlc_args()
        self.vlc_instance = vlc.Instance(*vlc_args)
        self.player = self.vlc_instance.media_player_new()

        # 8. Run UI construction
        self.setup_widgets()
        
        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', self.on_handle_drop)

        self.load_saved_paths()
        self.set_controls_enabled(False)
        
        # Periodic check of inactivity
        self.bind_all("<Any-KeyPress>", self.reset_idle_timer)
        self.bind_all("<Any-Button>", self.reset_idle_timer)
        self.bind("<Unmap>", self._on_minimized)
        self.bind("<Map>", self._on_restored)
        self.check_idle_and_state()

        # When closing the app, run the on_closing logic
        self.protocol("WM_DELETE_WINDOW", self._on_closing)


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
        
        self.db_target_label = ttk.Label(db_frame, text='No database loaded', wraplength=280, font=('', 9, 'bold'))
        self.db_target_label.pack(anchor='w')
        
        self.db_search_label = ttk.Label(db_frame, text='', wraplength=280, font=('', 9))
        self.db_search_label.pack(anchor='w')
        
        ttk.Separator(db_frame, orient='horizontal').pack(fill='x', pady=5)
        
        manage_db_button = ttk.Button(db_frame, text='Manage Databases...', command=self.open_db_manager)
        manage_db_button.pack(fill='x', pady=2)
        ToolTip(manage_db_button, 'Open the database manager to view, add, remove, and configure databases.')
        
        db_btn_frame = ttk.Frame(db_frame)
        db_btn_frame.pack(fill='x', pady=2)
        add_db_button = ttk.Button(db_btn_frame, text='Add Existing...', command=self.browse_existing_database)
        add_db_button.pack(side='left', expand=True, fill='x', padx=(0, 2))
        ToolTip(add_db_button, 'Add existing Scene Scout database (.db) files to the search list.')
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
        drop_label = ttk.Label(self.drop_area, text='Drag & Drop files/folders onto the GUI\nor click the buttons below to add to queue', 
                               anchor='center', justify='center')
        drop_label.pack(expand=True, fill='both')
        
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

        self.device_var.trace_add('write', lambda *args: self._update_standby_ui_state())
        self._update_standby_ui_state()

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

        # GPU Standby Toggle
        self.standby_check = ttk.Checkbutton(options_frame, text='GPU Standby when minimized', variable=self.gpu_standby_var, command=self._on_standby_toggle_changed)
        self.standby_check.pack(anchor='w', pady=(5, 0))
        ToolTip(self.standby_check, 'Offload model to CPU when minimized or idle to free VRAM.')

        self.device_var.trace_add('write', lambda *args: self._update_standby_ui_state())

        self._update_standby_ui_state()

        # Disable if current device is CPU
        if self.device_var.get() == 'cpu':
            self.standby_check.config(state='disabled')
        
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
        
        self.results_tree = ttk.Treeview(list_frame, columns=('filename','scene','time','source','score','rescore'), show='headings', selectmode='browse')
        for col, width in zip(['filename', 'scene', 'time', 'source', 'score', 'rescore'], [300, 80, 150, 140, 80, 80]):
            self.results_tree.heading(col, text=col.capitalize(), command=lambda c=col: self.sort_treeview(c))
            self.results_tree.column(col, width=width, anchor='center' if col not in ('filename', 'source') else 'w')
        
        self.results_tree.pack(side='left', fill='both', expand=True)
        list_scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.results_tree.yview)
        list_scrollbar.pack(side='right', fill='y')
        self.results_tree.config(yscrollcommand=list_scrollbar.set)
        self.results_tree.bind('<<TreeviewSelect>>', self.on_selection_change)
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

        self.export_btn = ttk.Button(preview_frame, text='Export Scene...', state='disabled', command=self.open_export_dialog)
        self.export_btn.pack(fill='x', padx=5, pady=(5, 0))
        ToolTip(self.export_btn, 'Export the selected scene as a video file.')

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

    def _update_db_section(self):
        if self.primary_db:
            target_name = os.path.basename(self.primary_db)
            self.db_target_label.config(text=f'\u2605 {target_name}')
        else:
            self.db_target_label.config(text='No target database set')
        
        search_count = len(self.active_databases)
        if search_count > 0:
            extra = search_count - 1 if self.primary_db in self.active_databases else search_count
            if extra > 0:
                self.db_search_label.config(text=f'+ {extra} additional search database(s)')
            else:
                self.db_search_label.config(text='')
        else:
            self.db_search_label.config(text='')

    def _add_databases(self, paths):
        added = []
        for path in paths:
            abs_path = str(Path(path).resolve())
            if abs_path not in self.active_databases:
                self.active_databases.append(abs_path)
                init_db(abs_path)
                added.append(abs_path)
        if added:
            if not self.primary_db:
                self.primary_db = added[0]
            self._update_db_section()
            self.save_db_config()
            self._update_button_states()
            self.update_status(f'Added {len(added)} database(s).')
            if self.db_manager_dlg and self.db_manager_dlg.winfo_exists():
                self.db_manager_dlg.refresh()

    def save_db_config(self):
        self.config['active_databases'] = self.active_databases
        self.config['primary_database'] = self.primary_db if self.primary_db else ''
        config.save_config(self.config)

    def open_db_manager(self):
        from database import get_db_stats
        
        dlg = tk.Toplevel(self)
        self.db_manager_dlg = dlg
        gui_utils.apply_window_icon(dlg, self.app_icon)
        dlg.title('Database Manager')
        dlg.transient(self)
        dlg.grab_set()
        dlg.minsize(700, 450)
        gui_utils.center_window(dlg, 700, 450)

        main_frame = ttk.Frame(dlg, padding=10)
        main_frame.pack(fill='both', expand=True)

        tree_frame = ttk.Frame(main_frame)
        tree_frame.pack(fill='both', expand=True, pady=(0, 10))

        columns = ('target', 'name', 'path', 'scenes', 'videos', 'images')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings',
                           selectmode='extended', height=10)
        tree.heading('target', text='')
        tree.heading('name', text='Name')
        tree.heading('path', text='Path')
        tree.heading('scenes', text='Scenes')
        tree.heading('videos', text='Videos')
        tree.heading('images', text='Images')

        tree.column('target', width=30, anchor='center')
        tree.column('name', width=160, anchor='w')
        tree.column('path', width=300, anchor='w')
        tree.column('scenes', width=60, anchor='center')
        tree.column('videos', width=60, anchor='center')
        tree.column('images', width=60, anchor='center')

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        item_to_db = {}

        def refresh_tree():
            for item in tree.get_children():
                tree.delete(item)
            item_to_db.clear()
            total_scenes = 0
            for db_path in self.active_databases:
                stats = get_db_stats(db_path)
                total_scenes += stats['scene_count']
                marker = '\u2605' if db_path == self.primary_db else ''
                iid = tree.insert('', 'end', values=(
                    marker,
                    os.path.basename(db_path),
                    db_path,
                    stats['scene_count'],
                    stats['video_count'],
                    stats['image_count']
                ))
                item_to_db[iid] = db_path
            update_status(total_scenes)

        dlg.refresh = refresh_tree

        def update_status(total_scenes=0):
            count = len(self.active_databases)
            text = f'{count} database(s) | {total_scenes:,} scenes total'
            status_var.set(text)

        status_var = tk.StringVar(master=dlg)
        ttk.Label(main_frame, textvariable=status_var, font=('Arial', 9, 'bold')).pack(anchor='w', pady=(0, 5))

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill='x', pady=5)

        def add_existing():
            paths = filedialog.askopenfilenames(title='Select Database Files', filetypes=[('SQLite Database', '*.db')])
            if paths:
                self._add_databases(paths)
                refresh_tree()

        def create_new():
            path = filedialog.asksaveasfilename(title='Create New Database', filetypes=[('SQLite Database', '*.db')], defaultextension='.db')
            if path:
                abs_path = str(Path(path).resolve())
                init_db(abs_path)
                self.active_databases.append(abs_path)
                if not self.primary_db:
                    self.primary_db = abs_path
                self.save_db_config()
                refresh_tree()

        def set_target():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning('Warning', 'No database selected.')
                return
            db_path = item_to_db.get(sel[0])
            if db_path:
                self.primary_db = db_path
                self.save_db_config()
                refresh_tree()
                self._update_db_section()
                self._update_button_states()
                self.update_queue_status()

        def remove_selected():
            selected_items = tree.selection()
            if not selected_items:
                messagebox.showwarning('Warning', 'No database selected.')
                return
            
            current_idx = tree.index(selected_items[0])
            
            if messagebox.askyesno('Confirm', f'Remove {len(selected_items)} selected database(s)?'):
                for item in selected_items:
                    db_path = item_to_db.get(item)
                    if db_path:
                        if db_path in self.active_databases:
                            self.active_databases.remove(db_path)
                        if self.primary_db == db_path:
                            self.primary_db = self.active_databases[0] if self.active_databases else None
                
                self.save_db_config()
                refresh_tree()
                
                children = tree.get_children()
                if children:
                    new_idx = min(current_idx, len(children) - 1)
                    tree.selection_set(children[new_idx])
                    tree.focus(children[new_idx])
                    tree.see(children[new_idx])
                
                self._update_db_section()
                self._update_button_states()
                self.update_queue_status()

        add_existing_btn = ttk.Button(btn_frame, text='Add Existing...', command=add_existing)
        add_existing_btn.pack(side='left', padx=(0, 5))
        ToolTip(add_existing_btn, 'Add existing Scene Scout database (.db) files to the search list.')
        
        create_new_btn = ttk.Button(btn_frame, text='Create New...', command=create_new)
        create_new_btn.pack(side='left', padx=5)
        ToolTip(create_new_btn, 'Create a new empty database and add it to the list.')
        
        set_target_btn = ttk.Button(btn_frame, text='Set Target', command=set_target)
        set_target_btn.pack(side='left', padx=5)
        ToolTip(set_target_btn, 'Set the selected database as the indexing/queue target.')
        
        remove_btn = ttk.Button(btn_frame, text='Remove', command=remove_selected)
        remove_btn.pack(side='left', padx=5)
        ToolTip(remove_btn, 'Remove the selected database from the search list.')
        
        def combine_all_databases():
            if not self.active_databases:
                messagebox.showwarning('Warning', 'No active databases to combine.')
                return
            
            out_path = filedialog.asksaveasfilename(
                title='Save Combined Database', 
                filetypes=[('SQLite Database', '*.db')], 
                defaultextension='.db'
            )
            
            if out_path:
                dlg.destroy()
                self.threaded_task(self._combine_task, out_path)

        combine_btn = ttk.Button(btn_frame, text='Combine All...', command=combine_all_databases)
        combine_btn.pack(side='left', padx=5)
        ToolTip(combine_btn, 'Merge all databases in the list into a single new file.')
        
        refresh_btn = ttk.Button(btn_frame, text='Refresh', command=refresh_tree)
        refresh_btn.pack(side='left', padx=5)
        ToolTip(refresh_btn, 'Re-query all databases for updated scene/video/image counts.')

        def on_double_click(event):
            item = tree.identify_row(event.y)
            if item:
                db_path = item_to_db.get(item)
                if db_path:
                    self.primary_db = db_path
                    self.save_db_config()
                    refresh_tree()
                    self._update_db_section()
                    self._update_button_states()
                    self.update_queue_status()

        tree.bind('<Double-1>', on_double_click)

        def show_context_menu(event):
            item = tree.identify_row(event.y)
            if not item:
                return
            if item not in tree.selection():
                tree.selection_set(item)
            menu = tk.Menu(tree, tearoff=0)
            menu.add_command(label='Set as Target', command=set_target)
            menu.add_separator()
            menu.add_command(label=f'Remove ({len(tree.selection())})', command=remove_selected)
            menu.post(event.x_root, event.y_root)

        tree.bind('<Button-3>', show_context_menu)

        dlg.bind('<Delete>', lambda e: remove_selected())

        refresh_tree()

        ttk.Button(main_frame, text='Close', command=dlg.destroy).pack(pady=(10, 0))

        def on_destroy():
            self.db_manager_dlg = None
            dlg.destroy()

        dlg.protocol('WM_DELETE_WINDOW', on_destroy)

    def _update_button_states(self):
        has_search_dbs = len(self.active_databases) > 0
        has_target = self.primary_db is not None
        model_loaded = self.model is not None
        
        if hasattr(self, 'search_button'):
            self.search_button.config(state='normal' if (has_search_dbs and model_loaded) else 'disabled')
        if hasattr(self, 'index_button'):
            self.index_button.config(state='normal' if has_target else 'disabled')
        if hasattr(self, 'load_model_button'):
            self.load_model_button.config(state='normal')
        if hasattr(self, 'rescore_button'):
            self.rescore_button.config(state='disabled')
        if hasattr(self, 'clear_rescore_button'):
            self.clear_rescore_button.config(state='disabled')
        if hasattr(self, 'query_text_entry'):
            self.query_text_entry.config(state='normal' if (has_search_dbs and model_loaded) else 'disabled')

    def _on_closing(self):
        """Cleanup resources before destroying the window."""
        self.is_active = False
        if self._idle_after_id:
            self.after_cancel(self._idle_after_id)
        self._stop_video_loop()
        self.destroy()

    def on_handle_drop(self, event):
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        
        db_paths = [p for p in paths if p.lower().endswith('.db')]
        if db_paths:
            self._add_databases(db_paths)
        
        for path in paths:
            if path.lower().endswith(config.IMAGE_EXTENSIONS):
                self.query_image_path = path
                self.query_image_var.set(os.path.basename(path))
                self.update_status(f"Query image set via drop: {os.path.basename(path)}")
                break
        
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
        from database import add_to_queue, queue_count
        
        active_dbs = self.config.get('active_databases', [])
        saved_primary = self.config.get('primary_database', '')
        
        valid_dbs = []
        missing_count = 0
        for db_path in active_dbs:
            if os.path.exists(db_path):
                abs_path = str(Path(db_path).resolve())
                if abs_path not in valid_dbs:
                    valid_dbs.append(abs_path)
            else:
                missing_count += 1
        
        self.active_databases = valid_dbs
        
        if saved_primary and saved_primary in valid_dbs:
            self.primary_db = saved_primary
        elif valid_dbs:
            self.primary_db = valid_dbs[0]
        else:
            self.primary_db = None
        
        if missing_count > 0:
            self.update_status(f'{missing_count} database(s) not found and removed from list.')
        elif valid_dbs:
            primary_name = os.path.basename(self.primary_db) if self.primary_db else 'None'
            self.update_status(f'Loaded {len(valid_dbs)} database(s). Target: {primary_name}')
        
        for db_path in self.active_databases:
            init_db(db_path)
        
        if 'folder_path' in self.config and self.config['folder_path'] and os.path.exists(self.config['folder_path']):
            if self.primary_db and queue_count(self.primary_db) == 0:
                add_to_queue(self.primary_db, self.config['folder_path'], is_directory=True, recursive=True)
            if 'folder_path' in self.config:
                del self.config['folder_path']
                config.save_config(self.config)
        
        self._update_db_section()
        self.update_queue_status()
        self._update_button_states()

    def update_queue_status(self):
        if not self.primary_db:
            self.queue_status_var.set('[0] items in queue (no target database)')
            self.index_button.config(state='disabled')
            return
        from database import queue_count
        count = queue_count(self.primary_db)
        self.queue_status_var.set(f'[{count}] items in queue')
        self.index_button.config(state='normal' if count > 0 else 'disabled')

    def on_queue_drop(self, event):
        """Handle drops on the dedicated drag-and-drop area."""
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        self._add_paths_to_queue(paths)

    def _add_paths_to_queue(self, paths):
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return
        from database import add_to_queue, queue_count
        added = 0
        for path in paths:
            if os.path.exists(path) and not path.lower().endswith('.db'):
                is_dir = os.path.isdir(path)
                if not is_dir and not path.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS):
                    continue
                add_to_queue(self.primary_db, path, is_directory=is_dir, recursive=is_dir)
                added += 1
        if added > 0:
            self.update_queue_status()
            self.update_status(f'Added {added} item(s) to queue.')
            if self.queue_manager_dlg and self.queue_manager_dlg.winfo_exists():
                self.queue_manager_dlg.refresh()
        elif paths:
            self.update_status('No valid media files or directories dropped.')

    def browse_files_dialog(self):
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return
        path = filedialog.askopenfilename(
            title='Select Media Files',
            filetypes=[('Media Files', ' '.join(f'*{ext}' for ext in config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS))],
            multiple=True
        )
        if path:
            self._add_paths_to_queue(path)

    def add_folder_to_queue(self):
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return
        path = filedialog.askdirectory(title='Select Folder to Add to Queue')
        if path:
            self._add_paths_to_queue([path])

    def add_files_to_queue(self):
        """Add individual files to the index queue."""
        self.browse_files_dialog()

    def open_queue_manager(self):
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return

        from database import get_queue, remove_from_queue, clear_queue, update_queue_recursive, queue_count

        dlg = tk.Toplevel(self)
        self.queue_manager_dlg = dlg
        gui_utils.apply_window_icon(dlg, self.app_icon)
        dlg.title('Queue Manager')
        dlg.transient(self)
        dlg.grab_set()
        dlg.minsize(900, 500)

        # Center window
        gui_utils.center_window(dlg, 900, 500)

        main_frame = ttk.Frame(dlg, padding=10)
        main_frame.pack(fill='both', expand=True)

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

        item_to_queue_id = {}

        def refresh_tree():
            for item in tree.get_children():
                tree.delete(item)
            item_to_queue_id.clear()
            queue_items = get_queue(self.primary_db)
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

        dlg.refresh = refresh_tree

        def update_status_label(missing=0):
            count = queue_count(self.primary_db)
            text = f'{count} item(s) in queue'
            if missing > 0:
                text += f' ({missing} missing)'
            status_var.set(text)

        status_var = tk.StringVar(master=self)
        ttk.Label(main_frame, textvariable=status_var, font=('Arial', 9, 'bold')).pack(anchor='w', pady=(0, 5))

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill='x', pady=5)

        def remove_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning('Warning', 'No items selected.')
                return
            
            current_idx = tree.index(selected[0])

            if messagebox.askyesno('Confirm', 'Remove %d selected item(s)?' % len(selected)):
                for iid in selected:
                    qid = item_to_queue_id.get(iid)
                    if qid:
                        remove_from_queue(self.primary_db, qid)
                refresh_tree()
                self.update_queue_status()
                
                children = tree.get_children()
                if children:
                    new_idx = min(current_idx, len(children) - 1)
                    tree.selection_set(children[new_idx])
                    tree.focus(children[new_idx])
                    tree.see(children[new_idx])

        def clear_all():
            if messagebox.askyesno('Confirm', 'Clear all items from the queue?'):
                clear_queue(self.primary_db)
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
                        update_queue_recursive(self.primary_db, qid, recursive_val)
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
                        remove_from_queue(self.primary_db, qid)
                refresh_tree()
                self.update_queue_status()

        remove_btn = ttk.Button(btn_frame, text='Remove Selected', command=remove_selected)
        remove_btn.pack(side='left', padx=(0, 5))
        ToolTip(remove_btn, 'Remove selected items from the queue')

        clear_btn = ttk.Button(btn_frame, text='Clear Queue', command=clear_all)
        clear_btn.pack(side='left', padx=5)
        ToolTip(clear_btn, 'Remove all items from the queue')

        clean_btn = ttk.Button(btn_frame, text='Clean Missing', command=clean_missing)
        clean_btn.pack(side='left', padx=5)
        ToolTip(clean_btn, 'Remove all items marked as [MISSING] from the queue')

        rec_on_btn = ttk.Button(btn_frame, text='Set Recursive ON',
                                command=lambda: toggle_recursive_for_selected(True))
        rec_on_btn.pack(side='left', padx=5)
        ToolTip(rec_on_btn, 'Enable recursive scanning for selected folders')

        rec_off_btn = ttk.Button(btn_frame, text='Set Recursive OFF',
                                 command=lambda: toggle_recursive_for_selected(False))
        rec_off_btn.pack(side='left', padx=5)
        ToolTip(rec_off_btn, 'Disable recursive scanning for selected folders')

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

        def on_key_press(event):
            if event.keysym in ('Delete', 'BackSpace'):
                remove_selected()

        dlg.bind('<KeyPress>', on_key_press)

        def on_tree_click(event):
            item = tree.identify_row(event.y)
            column = tree.identify_column(event.x)
            if item and column == '#4':
                qid = item_to_queue_id.get(item)
                values = tree.item(item)['values']
                if qid and values and values[0] == 'Folder':
                    new_rec = not (values[3] == 'Yes')
                    update_queue_recursive(self.primary_db, qid, new_rec)
                    refresh_tree()

        tree.bind('<ButtonRelease-1>', on_tree_click)

        refresh_tree()

        ttk.Button(main_frame, text='Close', command=dlg.destroy).pack(pady=(10, 0))

        def on_destroy():
            self.queue_manager_dlg = None
            dlg.destroy()

        dlg.protocol('WM_DELETE_WINDOW', on_destroy)

    def browse_database(self):
        path = filedialog.asksaveasfilename(parent=self, title='Create New Database File', initialdir='', filetypes=[('SQLite Database', '*.db')], defaultextension='.db')
        if path:
            abs_path = str(Path(path).resolve())
            init_db(abs_path)
            self.active_databases.append(abs_path)
            self.primary_db = abs_path
            self._update_db_section()
            self.save_db_config()
            self._update_button_states()
            self.update_queue_status()
            self.update_status(f'Database created and set as target: {os.path.basename(path)}')

    def browse_existing_database(self):
        paths = filedialog.askopenfilenames(parent=self, title='Select Existing Database Files', initialdir='', filetypes=[('SQLite Database', '*.db')])
        if paths:
            self._add_databases(paths)

    def browse_query_image(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[('Images', ' '.join((f'*{ext}' for ext in config.IMAGE_EXTENSIONS)))])
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
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return
        if messagebox.askyesno('Confirm', 'Remove entries for deleted files from the database?'):
            self.threaded_task(self._cleanup_task)

    def show_merging_popup(self):
        """Creates a non-blocking progress popup for the database merge."""
        self.merge_popup = tk.Toplevel(self)
        gui_utils.apply_window_icon(self.merge_popup, self.app_icon)
        self.merge_popup.title('Merging Databases')
        self.merge_popup.transient(self)
        self.merge_popup.grab_set()
        self.merge_popup.minsize(400, 150)
        
        frame = ttk.Frame(self.merge_popup, padding=20)
        frame.pack(fill='both', expand=True)
        
        self.merge_status_var = tk.StringVar(master=self.merge_popup, value='Starting merge...')
        ttk.Label(frame, textvariable=self.merge_status_var, font=('Arial', 10, 'bold')).pack(pady=(0, 10))
        
        self.merge_progress = ttk.Progressbar(frame, mode='indeterminate')
        self.merge_progress.pack(fill='x', pady=5)
        self.merge_progress.start(10)
        
        gui_utils.center_window(self.merge_popup, 400, 150)
        self.merge_popup.protocol('WM_DELETE_WINDOW', lambda: None)

    def update_merge_status(self, message: str):
        """Thread-safe update for the merge popup label."""
        if hasattr(self, 'merge_status_var'):
            self.after(0, lambda: self.merge_status_var.set(message))
        # Also update the main status bar
        self.update_status(message)

    def close_merging_popup(self):
        """Safely closes the merge popup and stops the progress bar."""
        if hasattr(self, 'merge_progress') and self.merge_progress:
            try:
                self.merge_progress.stop()
            except Exception:
                pass
                
        if hasattr(self, 'merge_popup') and self.merge_popup:
            try:
                self.merge_popup.destroy()
            except Exception:
                pass
            self.merge_popup = None

    def _combine_task(self, out_path: str):
        from database import combine_databases
        
        # 1. Initialize the popup on the main thread
        self.after(0, self.show_merging_popup)
        self.after(0, lambda: self.set_controls_enabled(False))
        
        try:
            # 2. Pass the UI update method as the callback
            combine_databases(self.active_databases, out_path, self.update_merge_status)
            
            self.after(0, lambda: self._add_databases([out_path]))
            self.after(0, lambda: messagebox.showinfo('Success', 'Databases combined successfully.'))
        except Exception as e:
            error_msg = str(e)
            self.after(0, lambda: messagebox.showerror('Merge Error', f'Failed to combine databases: {error_msg}'))
        finally:
            # 3. Clean up UI
            self.after(0, self.close_merging_popup)
            self.after(0, lambda: self.set_controls_enabled(True))
            self.update_status('Database merge complete.')

    def _cleanup_task(self):
        assert self.primary_db is not None
        self.update_status('Cleaning up database...')
        count = cleanup_orphaned_entries(self.primary_db, self.update_status)
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
        if not self.primary_db:
            messagebox.showerror('Error', 'Please select a target database first.')
            return
        from database import queue_count
        if queue_count(self.primary_db) == 0:
            messagebox.showerror('Error', 'Please add files or folders to the queue before indexing.')
            return
        self.ensure_model_active()
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
            self.index_filename_var.set('Cancelling...')
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
        assert self.primary_db is not None
        try:
            result = index_files(
                self.device, self.processor, self.model, self.primary_db,
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
            if self.primary_db:
                from database import clear_queue
                clear_queue(self.primary_db)
                self.update_queue_status()
            self.update_status('Indexing complete!')
            messagebox.showinfo('Complete', 'Indexing has finished.')

    def threaded_search(self):
        if not self.active_databases:
            messagebox.showerror('Error', 'Please add at least one database to search.')
            return
        assert self.active_databases, "No active databases"
        if db_is_empty(self.active_databases[0]):
            all_empty = all(db_is_empty(db) for db in self.active_databases)
            if all_empty:
                messagebox.showwarning('Warning', 'All active databases appear to be empty. Please index files before searching.')
                return
        if not self.query_text_var.get() and (not self.query_image_path):
            messagebox.showwarning('Warning', 'Please enter text or select an image to search.')
            return
        self.ensure_model_active()
        self.search_button.config(state='disabled')
        self._stop_video_loop()
        self.update_status('Searching...')
        self.threaded_task(self.search_task)

    def search_task(self):
        try:
            query_embedding = get_query_embedding(self.query_text_var.get(), self.query_image_path, self.device, self.processor, self.model, self.max_patches_var.get())
            if query_embedding is None:
                raise ValueError('Could not generate query embedding.')
            scene_results = search_scenes(query_embedding, self.active_databases, top_k=self.top_k_var.get())
            self.after(0, self.on_search_finished, scene_results)
        except Exception as e:
            self.after(0, lambda e=e: messagebox.showerror('Search Error', str(e)))
            print('Search Error', str(e))
        finally:
            self.after(0, lambda: self.search_button.config(state='normal'))

    def on_search_finished(self, results: List[Tuple[str, int, int, int, bytes, float, str]]):
        self.search_results = [(path, score, 'video', None, scene_idx, start_time, end_time, thumb_bytes, source_db)
                              for path, scene_idx, start_time, end_time, thumb_bytes, score, source_db in results]
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
    
    def _update_listview(self, preserve_sort=False):
        # 1. Clear existing rows and thumbnails
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)

        for widget in self.thumb_inner_frame.winfo_children():
            widget.destroy()

        self.thumbnail_references.clear()
        self.thumbnail_widgets = {} 
        visible_thumb_count = 0
        gc.collect() # Force garbage collection of old data

        if not self.search_results:
            self.stats_label.config(text='No results found.')
            return
        
        self.last_selected_entry = None
        has_rescore = self.search_results and self.search_results[0][3] is not None
        
        if not preserve_sort:
            self.current_sort_col = 'rescore' if has_rescore else 'score'
            self.current_sort_reverse = True
            sort_key = lambda x: x[3] if has_rescore else x[1]
            self.search_results.sort(key=sort_key, reverse=True)
            for c in self.results_tree['columns']:
                base_text = c.capitalize()
                if c == self.current_sort_col:
                    self.results_tree.heading(c, text=base_text + " \u25bc")
                else:
                    self.results_tree.heading(c, text=base_text)

        # 2. Populate the Treeview and Thumbnail Strip
        for i, data in enumerate(self.search_results, 1):
            path, score, ftype, rescore, scene_idx, scene_time, scene_end, thumb_bytes, source_db = data
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
            
            values = [filename, scene_str, time_str, source_db, f'{score:.4f}', f'{rescore:.4f}' if rescore is not None else '']
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
        scores = [rescore if has_rescore and rescore is not None else score for _, score, _, rescore, _, _, _, _, _ in self.search_results]
        stats_text = f'Found {len(scores)} results | Max: {max(scores):.3f} | Avg: {np.mean(scores):.3f}'
        self.stats_label.config(text=stats_text)
        
        first = self.results_tree.get_children()
        if first:
            self.results_tree.selection_set(first[0])
            self.on_result_select(None)

    def sort_treeview(self, col):
        if not self.search_results:
            return

        if self.current_sort_col == col:
            self.current_sort_reverse = not self.current_sort_reverse
        else:
            self.current_sort_col = col
            self.current_sort_reverse = col in ('score', 'rescore')

        def get_sort_key(item):
            if col == 'filename':
                return os.path.basename(item[0]).lower()
            elif col == 'scene':
                return item[4] if item[4] is not None else -1
            elif col == 'time':
                return item[5] if item[5] is not None else -1
            elif col == 'source':
                return item[8].lower() if item[8] else ""
            elif col == 'rescore':
                return item[3] if item[3] is not None else item[1]
            else:
                return item[1]

        self.search_results.sort(key=get_sort_key, reverse=self.current_sort_reverse)
        self._update_listview(preserve_sort=True)

        for c in self.results_tree['columns']:
            base_text = c.capitalize()
            if c == self.current_sort_col:
                arrow = " \u25bc" if self.current_sort_reverse else " \u25b2"
                self.results_tree.heading(c, text=base_text + arrow)
            else:
                self.results_tree.heading(c, text=base_text)

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
        assert self.primary_db is not None
        self.update_status(f"Rescoring with: '{query_text}'...")
        try:
            rescore_embedding = get_query_embedding(query_text, None, self.device, self.processor, self.model)
            if rescore_embedding is None:
                raise ValueError('Could not generate rescore embedding.')
                
            with sqlite3.connect(self.primary_db) as conn:
                cursor = conn.cursor()
                for i, (path, score, ftype, _, scene_idx, scene_time, scene_end, thumb_bytes, source_db) in enumerate(self.search_results):
                    
                    if ftype == 'image':
                        cursor.execute('SELECT embedding FROM image_embeddings WHERE filepath=?', (path,))
                        result = cursor.fetchone()
                        if result:
                            embedding = np.frombuffer(result[0], dtype=np.float32)
                            similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                            self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end, thumb_bytes, source_db)
                            
                    elif ftype == 'video':
                        if isinstance(scene_idx, tuple):
                            start_idx, end_idx = scene_idx
                            cursor.execute(
                                'SELECT embedding FROM scene_embeddings WHERE filepath=? AND scene_index >= ? AND scene_index <= ?', 
                                (path, start_idx, end_idx)
                            )
                            results = cursor.fetchall()
                            if results:
                                max_sim = -1.0
                                for res in results:
                                    emb = np.frombuffer(res[0], dtype=np.float32)
                                    sim = float(np.dot(emb, rescore_embedding.T).squeeze())
                                    if sim > max_sim:
                                        max_sim = sim
                                self.search_results[i] = (path, score, ftype, max_sim, scene_idx, scene_time, scene_end, thumb_bytes, source_db)
                        else:
                            cursor.execute('SELECT embedding FROM scene_embeddings WHERE filepath=? AND scene_index=?', (path, scene_idx))
                            result = cursor.fetchone()
                            if result:
                                embedding = np.frombuffer(result[0], dtype=np.float32)
                                similarity = np.dot(embedding, rescore_embedding.T).squeeze()
                                self.search_results[i] = (path, score, ftype, float(similarity), scene_idx, scene_time, scene_end, thumb_bytes, source_db)
                                
            self.after(0, self.on_rescore_finished)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('Rescore Error', str(e)))

    def on_rescore_finished(self):
        self._update_listview()
        self.update_status('Rescore complete.')
        self.clear_rescore_button.config(state='normal')

    def clear_rescore(self):
        self.search_results = [(path, score, ftype, None, scene_idx, scene_time, scene_end, thumb_bytes, source_db) for path, score, ftype, _, scene_idx, scene_time, scene_end, thumb_bytes, source_db in self.search_results]
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
            
            # Extract metadata from search_results (matches the 9-item tuple structure)
            # Structure: path, score, ftype, rescore, scene_idx, start_ms, end_ms, thumb_bytes, source_db
            path, _, file_type, _, _, scene_time, scene_end, _, _ = self.search_results[index]
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

        sel = self.results_tree.selection()
        menu = tk.Menu(self, tearoff=0)

        if sel:
            index = int(sel[0])
            file_type = self.search_results[index][2]
            start_ms = self.search_results[index][5]
            end_ms = self.search_results[index][6]

            if file_type == 'video' and start_ms is not None and end_ms is not None:
                menu.add_command(label='Export Scene...',
                               command=lambda: self.open_export_dialog_for_index(index))
                menu.add_separator()

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
        file_type = self.search_results[index][2]
        start_ms = self.search_results[index][5]
        end_ms = self.search_results[index][6]
        self.current_display_path = path
        menu = tk.Menu(self, tearoff=0)

        if file_type == 'video' and start_ms is not None and end_ms is not None:
            menu.add_command(label='Export Scene...',
                           command=lambda: self.open_export_dialog_for_index(index))
            menu.add_separator()

        menu.add_command(label='Copy Path', command=lambda p=path: self.clipboard_append(p))
        menu.add_command(label='Open Containing Folder', command=self.open_containing_folder)
        menu.add_command(label='Open File', command=self.open_current_file)
        menu.post(event.x_root, event.y_root)

    def on_selection_change(self, event: tk.Event):
        """Handle treeview selection change to update export button state."""
        sel = self.results_tree.selection()
        if not sel:
            self.export_btn.config(state='disabled')
            return

        index = int(sel[0])
        if index < len(self.search_results):
            file_type = self.search_results[index][2]
            start_ms = self.search_results[index][5]
            end_ms = self.search_results[index][6]

            if file_type == 'video' and start_ms is not None and end_ms is not None:
                self.export_btn.config(state='normal')
            else:
                self.export_btn.config(state='disabled')
        else:
            self.export_btn.config(state='disabled')

        # Call the original on_result_select to display media
        self.on_result_select(event)

    def open_export_dialog(self):
        """Open export dialog for the currently selected scene."""
        sel = self.results_tree.selection()
        if not sel:
            return
        # sel[0] is the tree iid, which we use as the index
        self.open_export_dialog_for_index(int(sel[0]))

    def open_export_dialog_for_index(self, index: int):
        """Open export dialog for a specific search result index."""
        if index >= len(self.search_results):
            return

        path = self.search_results[index][0]
        start_ms = self.search_results[index][5]
        end_ms = self.search_results[index][6]

        # 1. Save the user's current playback preference
        original_playback_state = config.SCENE_PLAYBACK

        # 2. Temporarily disable playback to show a static frame and release the VLC file lock
        if original_playback_state:
            config.SCENE_PLAYBACK = False
            self.display_media(path, is_video=True, start_ms=start_ms, end_ms=end_ms)
        else:
            self._stop_video_loop()

        from exporter import SceneExportDialog
        dialog = SceneExportDialog(self, path, start_ms, end_ms)
        
        # 3. Yield the event loop until the export dialog is closed/destroyed
        self.wait_window(dialog)
        
        # 4. Restore the playback preference once the dialog closes
        if original_playback_state:
            config.SCENE_PLAYBACK = True
            
            # Restart the video playback if the same item is still selected
            sel = self.results_tree.selection()
            if sel and int(sel[0]) == index:
                self.display_media(path, is_video=True, start_ms=start_ms, end_ms=end_ms)

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
    
    def toggle_model_standby(self, to_cpu: bool):
        """Moves the model between GPU and CPU to manage resources."""
        if self.model is None or self.device.type != 'cuda':
            return

        if to_cpu and not self._is_in_standby:
            self.update_status("Entering Standby: Moving model to CPU...")
            self.model.to('cpu')
            torch.cuda.empty_cache()
            self._is_in_standby = True
            self.update_status("Standby VRAM Freed")
        
        elif not to_cpu and self._is_in_standby:
            self.update_status("Waking Up: Moving model to GPU...")
            self.model.to(self.device)
            self._is_in_standby = False
            self.update_status("Model Active (GPU)")

    def reset_idle_timer(self, event=None):
        """Resets the idle counter on user input."""
        self._idle_counter = 0
        if self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)

    def check_idle_and_state(self):
        """Periodic background loop for time-based offloading."""
        if not self.is_active:
            return

        # Increment counter
        self._idle_counter += 1
        
        # Retrieve idle limit from config (default to 300s / 5 mins)
        idle_limit = self.config.get('idle_offload_seconds', 300)

        # Trigger standby if the feature is enabled and threshold is reached
        if self.gpu_standby_var.get() and self._idle_counter >= idle_limit:
            if not self._is_in_standby:
                self.toggle_model_standby(to_cpu=True)

        # Reschedule for 1 second from now
        self._idle_after_id = self.after(1000, self.check_idle_and_state)
    
    def _update_standby_ui_state(self):
        """Updates the toggle availability based on selected hardware."""
        if not hasattr(self, 'standby_check'):
            return
        if self.device_var.get() == 'cpu':
            self.standby_check.config(state='disabled')
        else:
            self.standby_check.config(state='normal')

    def _on_minimized(self, event=None):
        # Only trigger if the event is for the main window itself and feature is enabled
        if event.widget == self and self.gpu_standby_var.get():
            self.toggle_model_standby(to_cpu=True)

    def _on_restored(self, event=None):
        if event.widget == self:
            self._idle_counter = 0  # Reset idle on return
            self.toggle_model_standby(to_cpu=False)

    def _on_standby_toggle_changed(self):
        """Handles logic when user clicks the GUI toggle."""
        val = self.gpu_standby_var.get()
        self.save_config_key('gpu_standby', val)
        
        # If turned OFF while in standby, wake up immediately
        if not val and self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)

    def ensure_model_active(self):
        """Guarantees the model is on the GPU before a heavy task begins."""
        if self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)
        self._idle_counter = 0 # Reset idle counter so it doesn't sleep mid-task