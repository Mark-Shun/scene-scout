import os
import io
import sys
import gc
import subprocess
import webbrowser
from pathlib import Path
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, QEvent, Signal, Slot, QSize, QAbstractTableModel, QSortFilterProxyModel, QModelIndex, QItemSelectionModel
from PySide6.QtGui import QIcon, QPixmap, QAction, QClipboard
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QMessageBox, QFileDialog, QInputDialog,
    QProgressBar, QLabel, QPushButton, QDialog,
    QFrame, QScrollArea, QGridLayout, QComboBox, QSpinBox,
    QCheckBox, QRadioButton, QButtonGroup, QLineEdit,
    QGroupBox, QTableView, QHeaderView, QStackedWidget,
    QMenu, QSizePolicy, QAbstractItemView,
    QProgressDialog, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem
)
import numpy as np
import torch

import config
import gui_utils
from workers import (
    SignalBridge, ModelLoadWorker, IndexWorker, SearchWorker,
    RescoreWorker, CombineDBWorker, VerifyPathsWorker, CleanupWorker,
)

try:
    import torch_directml
except ImportError:
    torch_directml = None

try:
    import intel_extension_for_pytorch as ipex
except ImportError:
    ipex = None

try:
    import vlc
except Exception:
    import traceback
    traceback.print_exc()
    print('A VLC installation is needed for the GUI. Please install VLC before starting Scene Scout.', file=sys.stderr)
    sys.exit(1)

from PIL import Image
from model_loader import load_siglip_model, get_compute_device, TRT_AVAILABLE
from database import init_db, db_is_empty


# ---------------------------------------------------------------------------
# Drag-and-Drop Zone
# ---------------------------------------------------------------------------

class DropZoneFrame(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(60)
        self.setObjectName('DropZoneFrame')
        self.setStyleSheet("""
            DropZoneFrame#DropZoneFrame {
                border: 2px dashed rgba(128, 128, 128, 0.5);
                border-radius: 5px;
                background-color: rgba(128, 128, 128, 0.08);
            }
        """)
        layout = QVBoxLayout(self)
        self._label = QLabel(
            "Drag & Drop files/folders onto the GUI\n"
            "or click the buttons below to add to queue"
        )
        self._label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._label)

        self.click_enabled = True

    def mousePressEvent(self, event):
        if self.click_enabled:
            self.files_dropped.emit([])
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if paths:
            self.files_dropped.emit(paths)


# ---------------------------------------------------------------------------
# Search Results Table Model
# ---------------------------------------------------------------------------

class SearchResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []
        self._headers = ["Filename", "Scene", "Time", "Source", "Score", "Rescore"]

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row >= len(self._data) or col >= len(self._headers):
            return None
        entry = self._data[row]
        if role == Qt.DisplayRole:
            return entry[col]
        if role == Qt.UserRole:
            return entry[col]
        if role == Qt.ToolTipRole:
            if col == 0:
                return entry[0]
            return entry[col]
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._headers[section]
        return None

    def update_data(self, new_data):
        self.beginResetModel()
        self._data = new_data
        self.endResetModel()


class SearchSortProxy(QSortFilterProxyModel):
    def lessThan(self, left, right):
        if left.column() in (1, 4, 5):
            left_val = left.data(Qt.UserRole)
            right_val = right.data(Qt.UserRole)
            if left_val is not None and right_val is not None:
                try:
                    return float(left_val) < float(right_val)
                except (ValueError, TypeError):
                    pass
        return super().lessThan(left, right)


# ---------------------------------------------------------------------------
# Indexing Progress Dialog
# ---------------------------------------------------------------------------

class IndexProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Processing Media')
        self.setMinimumWidth(500)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        self.status_label = QLabel('Initializing...')
        self.status_label.setStyleSheet('font-weight: bold; font-size: 14px;')
        layout.addWidget(self.status_label)

        self.file_label = QLabel('Starting up...')
        self.file_label.setStyleSheet('color: gray;')
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.count_label = QLabel('0 / 0')
        self.count_label.setAlignment(Qt.AlignRight)
        layout.addWidget(self.count_label)

        self.cancel_btn = QPushButton('Cancel Processing')
        self.cancel_btn.setMinimumWidth(180)
        self.cancel_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(self.cancel_btn, alignment=Qt.AlignCenter)


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------

class SceneScoutApp(QMainWindow):
    vlc_end_reached = Signal()

    def __init__(self):
        super().__init__()
        self.is_active = True
        self._is_background_task_running = False
        self.setWindowTitle('Scene Scout')
        self.resize(1200, 800)

        app_icon = gui_utils.load_app_icon(config.big_logo)
        self.setWindowIcon(app_icon)

        # Config
        self.config = config.load_config()
        config.SCENE_PLAYBACK = self.config['scene_playback']

        # GPU standby
        self._is_in_standby = False
        self._idle_counter = 0
        self._idle_timer = None

        # Device detection
        saved_device = self.config.get('device')
        device_str, device_msg, _, _ = get_compute_device(saved_device)
        self.device_msg = device_msg
        self.device_choice = device_str
        self.show_trt_option = (device_str == 'cuda' and TRT_AVAILABLE)

        # Internal state
        self.model = None
        self.processor = None
        self.device = None
        self.dtype = None
        self._last_active_device = None
        self.active_databases: List[str] = []
        self.primary_db: Optional[str] = None
        self.db_manager_dlg = None
        self.queue_manager_dlg = None
        self.query_image_path = None
        self.search_results = []
        self.last_selected_entry = None
        self.current_sort_col = 'score'
        self.current_sort_reverse = True
        self.original_image = None
        self.current_display_path = None
        self.current_media = None

        # VLC setup
        vlc_args = config.get_vlc_args()
        self.vlc_instance = vlc.Instance(*vlc_args)
        self.player = self.vlc_instance.media_player_new()


        # ---- Build the UI ----
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(4)
        self.splitter.setStyleSheet("QSplitter::handle { background: rgba(128, 128, 128, 0.2); }")
        main_layout.addWidget(self.splitter)

        # Left panel (scrollable controls)
        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.NoFrame)
        self.left_panel = QWidget()
        self.left_scroll.setWidget(self.left_panel)
        self.splitter.addWidget(self.left_scroll)

        # Right panel (results + preview)
        self.right_panel = QWidget()
        self.splitter.addWidget(self.right_panel)

        self.splitter.setSizes([340, 860])

        self.setup_left_panel()
        self.setup_right_panel()

        # Worker / signal bridge
        self._signal_bridge = SignalBridge()
        self._current_worker = None
        self._index_worker = None

        # Accept drops on main window for global handling
        self.setAcceptDrops(True)

        # Status bar permanent widgets (must init before load_saved_paths)
        self._statusbar_db_label = QLabel()
        self._statusbar_db_label.setStyleSheet('padding: 0 8px; font-weight: bold;')
        self.statusBar().addPermanentWidget(self._statusbar_db_label)

        self._statusbar_count_label = QLabel()
        self._statusbar_count_label.setStyleSheet('padding: 0 8px;')
        self.statusBar().addPermanentWidget(self._statusbar_count_label)

        # Load saved databases
        self.load_saved_paths()

        # GPU standby periodic check
        self.check_idle_and_state()

    # ======================================================================
    # Drag-and-Drop (global handler)
    # ======================================================================

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            valid_exts = config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS + ('.db',)
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if os.path.isdir(path) or path.lower().endswith(valid_exts):
                    event.acceptProposedAction()
                    return
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if not paths:
            return
        db_paths = [p for p in paths if p.lower().endswith('.db')]
        if db_paths:
            self._add_databases(db_paths)
        for path in paths:
            if path.lower().endswith(config.IMAGE_EXTENSIONS):
                self.query_image_path = path
                if hasattr(self, '_query_image_label') and self._query_image_label:
                    self._query_image_label.setText(os.path.basename(path))
                self.update_status(f"Query image set via drop: {os.path.basename(path)}")
                break
        media_paths = [p for p in paths if not p.lower().endswith('.db') and (
            os.path.isdir(p) or p.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS)
        )]
        if media_paths:
            self._add_paths_to_queue(media_paths)

    # ======================================================================
    # Close
    # ======================================================================

    def closeEvent(self, event):
        self.is_active = False
        if self._idle_timer:
            self._idle_timer.stop()
        if self._current_worker and self._current_worker.isRunning():
            self._current_worker.requestInterruption()
            self._current_worker.wait(2000)
        self._stop_video_loop()
        event.accept()

    # ======================================================================
    # Left Panel Construction
    # ======================================================================

    def setup_left_panel(self):
        layout = QVBoxLayout(self.left_panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)

        # ---- Database Section ----
        db_group = QGroupBox("Database")
        db_layout = QVBoxLayout(db_group)

        self._db_target_label = QLabel('No database loaded')
        self._db_target_label.setWordWrap(True)
        self._db_target_label.setStyleSheet('font-weight: bold;')
        db_layout.addWidget(self._db_target_label)

        self._db_search_label = QLabel('')
        self._db_search_label.setWordWrap(True)
        db_layout.addWidget(self._db_search_label)

        manage_btn = QPushButton('Manage Databases...')
        manage_btn.clicked.connect(self.open_db_manager)
        manage_btn.setToolTip('Open the database manager to view, add, remove, and configure databases.')
        db_layout.addWidget(manage_btn)

        db_btn_row = QHBoxLayout()
        add_existing_btn = QPushButton('Add Existing...')
        add_existing_btn.clicked.connect(self.browse_existing_database)
        add_existing_btn.setToolTip('Add existing Scene Scout database (.db) files to the search list.')
        db_btn_row.addWidget(add_existing_btn)

        create_btn = QPushButton('Create New...')
        create_btn.clicked.connect(self.browse_database)
        create_btn.setToolTip('Create a new database for indexing media files.')
        db_btn_row.addWidget(create_btn)
        db_layout.addLayout(db_btn_row)

        layout.addWidget(db_group)

        # ---- Media Queue Section ----
        queue_group = QGroupBox("Media Queue")
        queue_layout = QVBoxLayout(queue_group)

        self._drop_zone = DropZoneFrame()
        self._drop_zone.files_dropped.connect(self._on_dropzone_drop)
        self._drop_zone.click_enabled = True
        queue_layout.addWidget(self._drop_zone)

        self._queue_status_label = QLabel('[0] items in queue')
        self._queue_status_label.setStyleSheet('font-weight: bold;')
        queue_layout.addWidget(self._queue_status_label)

        queue_btn_row = QHBoxLayout()
        add_folder_btn = QPushButton('Add Folder(s)')
        add_folder_btn.clicked.connect(self.add_folder_to_queue)
        add_folder_btn.setToolTip('Add a directory to the index queue. Recursive by default.')
        queue_btn_row.addWidget(add_folder_btn)

        add_file_btn = QPushButton('Add File(s)')
        add_file_btn.clicked.connect(self.add_files_to_queue)
        add_file_btn.setToolTip('Add individual media files to the index queue.')
        queue_btn_row.addWidget(add_file_btn)
        queue_layout.addLayout(queue_btn_row)

        inspect_btn = QPushButton('Inspect Queue...')
        inspect_btn.clicked.connect(self.open_queue_manager)
        inspect_btn.setToolTip('Open the queue manager to view, modify, or remove queued items.')
        queue_layout.addWidget(inspect_btn)

        self._index_button = QPushButton('Process Media')
        self._index_button.clicked.connect(self.threaded_index)
        self._index_button.setToolTip('Process all files in the queue and update the scene database.')
        self._index_button.setEnabled(False)
        queue_layout.addWidget(self._index_button)

        layout.addWidget(queue_group)

        # ---- Search Query Section ----
        query_group = QGroupBox("Search Query")
        query_layout = QVBoxLayout(query_group)

        query_layout.addWidget(QLabel('Text:'))
        self._query_text_edit = QLineEdit()
        self._query_text_edit.setPlaceholderText('Enter natural language text to search for matching scenes.')
        self._query_text_edit.returnPressed.connect(self.threaded_search)
        query_layout.addWidget(self._query_text_edit)

        self._query_image_label = QLabel('No query image')
        self._query_image_label.setWordWrap(True)
        query_layout.addWidget(self._query_image_label)

        query_btn_row = QHBoxLayout()
        load_query_btn = QPushButton('Load...')
        load_query_btn.clicked.connect(self.browse_query_image)
        load_query_btn.setToolTip('Load an image to use as the search query.')
        query_btn_row.addWidget(load_query_btn)

        clear_query_btn = QPushButton('Clear')
        clear_query_btn.clicked.connect(self.clear_query_image)
        clear_query_btn.setToolTip('Clear the current query image from the search form.')
        query_btn_row.addWidget(clear_query_btn)
        query_layout.addLayout(query_btn_row)

        self._search_button = QPushButton('Search Scene')
        self._search_button.clicked.connect(self.threaded_search)
        self._search_button.setToolTip('Run the search using the current text and/or image query.')
        self._search_button.setEnabled(False)
        query_layout.addWidget(self._search_button)

        layout.addWidget(query_group)

        # ---- Options Section ----
        options_group = QGroupBox("Options")
        opts_layout = QVBoxLayout(options_group)

        opts_layout.addWidget(QLabel('Compute Device:'))
        device_options = ['cpu']
        if torch.cuda.is_available():
            device_options.append('cuda')
        if torch_directml is not None and torch_directml.is_available():
            device_options.append('dml')
        if ipex is not None and hasattr(torch, 'xpu') and torch.xpu.is_available():
            device_options.append('xpu')
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device_options.append('mps')

        self._device_combobox = QComboBox()
        self._device_combobox.addItems(device_options)
        idx = self._device_combobox.findText(self.device_choice)
        if idx >= 0:
            self._device_combobox.setCurrentIndex(idx)
        self._device_combobox.currentTextChanged.connect(self._on_device_changed)
        opts_layout.addWidget(self._device_combobox)

        device_msg_label = QLabel(f'Auto-detected: {self.device_msg}')
        device_msg_label.setStyleSheet('font-style: italic; font-size: 8pt;')
        opts_layout.addWidget(device_msg_label)

        if self.show_trt_option:
            self._trt_check = QCheckBox('Use TensorRT Acceleration')
            self._trt_check.setChecked(self.config.get('use_trt', False))
            self._trt_check.toggled.connect(self.save_trt_preference)
            opts_layout.addWidget(self._trt_check)

        opts_layout.addWidget(QLabel('Detection method:'))
        detect_frame = QFrame()
        detect_layout = QHBoxLayout(detect_frame)
        detect_layout.setContentsMargins(0, 0, 0, 0)

        self._fast_radio = QRadioButton('Fast')
        self._fast_radio.setChecked(self.config.get('fast_detect', True))
        self._fast_radio.toggled.connect(lambda checked: self._on_detect_method_changed(checked))

        self._accurate_radio = QRadioButton('Accurate')
        self._accurate_radio.setChecked(not self.config.get('fast_detect', True))

        detect_layout.addWidget(self._fast_radio)
        detect_layout.addWidget(self._accurate_radio)
        opts_layout.addWidget(detect_frame)

        opts_layout.addWidget(QLabel('Max patches:'))
        self._max_patches_spin = QSpinBox()
        self._max_patches_spin.setRange(128, 1024)
        self._max_patches_spin.setSingleStep(128)
        self._max_patches_spin.setValue(self.config.get('max_patches', 256))
        self._max_patches_spin.valueChanged.connect(lambda v: self.save_config_key('max_patches', v))
        self._max_patches_spin.setToolTip('Number of patches to evaluate per scene; higher values may improve accuracy but increase runtime.')
        opts_layout.addWidget(self._max_patches_spin)

        opts_layout.addWidget(QLabel('Frames to pool per scene:'))
        self._frames_pool_spin = QSpinBox()
        self._frames_pool_spin.setRange(1, 10)
        self._frames_pool_spin.setValue(self.config.get('frames_per_scene', 3))
        self._frames_pool_spin.valueChanged.connect(lambda v: self.save_config_key('frames_per_scene', v))
        self._frames_pool_spin.setToolTip('Extracts N frames evenly across a scene and combines them (Max Pooling) for higher accuracy. 1 is fastest, 3-5 is optimal.')
        opts_layout.addWidget(self._frames_pool_spin)

        opts_layout.addWidget(QLabel('Results:'))
        self._top_k_spin = QSpinBox()
        self._top_k_spin.setRange(1, 100)
        self._top_k_spin.setValue(self.config.get('top_k', 20))
        self._top_k_spin.valueChanged.connect(lambda v: self.save_config_key('top_k', v))
        self._top_k_spin.setToolTip('How many matching scenes to return for each search.')
        opts_layout.addWidget(self._top_k_spin)

        opts_layout.addWidget(QLabel('Scene embed batch size:'))
        self._batch_size_spin = QSpinBox()
        self._batch_size_spin.setRange(8, 160)
        self._batch_size_spin.setValue(self.config.get('batch_size', 16))
        self._batch_size_spin.valueChanged.connect(lambda v: self.save_config_key('batch_size', v))
        self._batch_size_spin.setToolTip('Number of images processed at once when computing scene embeddings.')
        opts_layout.addWidget(self._batch_size_spin)

        self._vlc_open_check = QCheckBox('Open video in VLC')
        self._vlc_open_check.setChecked(self.config.get('use_vlc_open', True))
        self._vlc_open_check.toggled.connect(lambda v: self.save_config_key('use_vlc_open', v))
        opts_layout.addWidget(self._vlc_open_check)

        self._thumb_check = QCheckBox('Generate Thumbnails (increases DB size)')
        self._thumb_check.setChecked(self.config.get('generate_thumbnails', True))
        self._thumb_check.toggled.connect(lambda v: self.save_config_key('generate_thumbnails', v))
        opts_layout.addWidget(self._thumb_check)

        self._reprocess_check = QCheckBox('Force overwrite/reprocess indexed files')
        self._reprocess_check.setChecked(self.config.get('force_reprocess', False))
        self._reprocess_check.toggled.connect(lambda v: self.save_config_key('force_reprocess', v))
        self._reprocess_check.setToolTip('If checked, queued files that already exist in the database will be re-analyzed and overwritten using the current settings.')
        opts_layout.addWidget(self._reprocess_check)

        self._standby_check = QCheckBox('GPU Standby when minimized')
        self._standby_check.setChecked(self.config.get('gpu_standby', True))
        self._standby_check.toggled.connect(self._on_standby_toggle_changed)
        self._standby_check.setToolTip('Offload model to CPU when minimized or idle to free VRAM.')
        opts_layout.addWidget(self._standby_check)

        if self.device_choice == 'cpu':
            self._standby_check.setEnabled(False)

        theme_frame = QFrame()
        theme_layout = QHBoxLayout(theme_frame)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        self._theme_combobox = QComboBox()

        default_theme = config.DEFAULT_CONFIG.get('theme', 'dark_lightgreen.xml')
        saved_theme = self.config.get('theme', default_theme)

        target_index = -1
        fallback_index = 0

        for idx, filename in enumerate(self._get_available_themes()):
            display_name = filename.split('.')[0].replace('_', ' ').title()
            self._theme_combobox.addItem(display_name, userData=filename)
            if filename == saved_theme:
                target_index = idx
            if filename == default_theme:
                fallback_index = idx

        self._theme_combobox.setCurrentIndex(target_index if target_index != -1 else fallback_index)
        theme_layout.addWidget(self._theme_combobox)

        apply_theme_btn = QPushButton('Apply')
        apply_theme_btn.clicked.connect(self.apply_theme)
        theme_layout.addWidget(apply_theme_btn)
        opts_layout.addWidget(theme_frame)

        layout.addWidget(options_group)

        # ---- Additional Actions ----
        actions_group = QGroupBox("Additional Actions")
        actions_layout = QVBoxLayout(actions_group)

        self._load_model_button = QPushButton('Load Model')
        self._load_model_button.clicked.connect(self.threaded_load_model)
        actions_layout.addWidget(self._load_model_button)

        cleanup_btn = QPushButton('Cleanup Database')
        cleanup_btn.clicked.connect(self.cleanup_database)
        actions_layout.addWidget(cleanup_btn)

        layout.addWidget(actions_group)

        # ---- Info ----
        info_group = QGroupBox("Info")
        info_layout = QVBoxLayout(info_group)
        about_btn = QPushButton('About')
        about_btn.clicked.connect(self.open_about_dialog)
        info_layout.addWidget(about_btn)
        layout.addWidget(info_group)

        # ---- Status ----
        self._status_label = QLabel('Initializing...')
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet('padding: 5px;')
        layout.addWidget(self._status_label)

        layout.addStretch()

    def _get_available_themes(self):
        return [
            # --- Custom QSS Themes ---
            "default_light.qss",
            "fruitiger_aero.qss",
            "luna_blue.qss",
            "sakura.qss",
            "matrix.qss",
            "ubuntu.qss",
            "win2k.qss",
            # --- qt-material themes ---
            "dark_teal.xml",
            "dark_amber.xml",
            "dark_blue.xml",
            "dark_cyan.xml",
            "dark_lightgreen.xml",
            "dark_pink.xml",
            "dark_purple.xml",
            "dark_red.xml",
            "light_teal.xml",
            "light_amber.xml",
            "light_blue.xml",
            "light_cyan.xml",
            "light_pink.xml",
            "light_purple.xml",
        ]

    # ======================================================================
    # Right Panel Construction
    # ======================================================================

    def setup_right_panel(self):
        layout = QVBoxLayout(self.right_panel)
        layout.setContentsMargins(10, 10, 10, 10)

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.setHandleWidth(4)
        right_splitter.setStyleSheet("QSplitter::handle { background: rgba(128, 128, 128, 0.2); }")

        # ---- Results Section ----
        results_container = QWidget()
        results_layout = QVBoxLayout(results_container)
        results_layout.setContentsMargins(0, 0, 0, 0)

        self._stats_label = QLabel('No search performed')
        results_layout.addWidget(self._stats_label)

        rescore_frame = QFrame()
        rescore_row = QHBoxLayout(rescore_frame)
        rescore_row.setContentsMargins(0, 0, 0, 0)

        self._rescore_button = QPushButton('Rescore...')
        self._rescore_button.clicked.connect(self.open_rescore_dialog)
        self._rescore_button.setEnabled(False)
        rescore_row.addWidget(self._rescore_button)

        self._clear_rescore_button = QPushButton('Clear Rescore')
        self._clear_rescore_button.clicked.connect(self.clear_rescore)
        self._clear_rescore_button.setEnabled(False)
        rescore_row.addWidget(self._clear_rescore_button)

        rescore_row.addStretch()
        results_layout.addWidget(rescore_frame)

        self.results_model = SearchResultsModel(self)
        self.results_proxy = SearchSortProxy(self)
        self.results_proxy.setSourceModel(self.results_model)

        self._results_table = QTableView()
        self._results_table.setModel(self.results_proxy)
        self._results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._results_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._results_table.setSortingEnabled(True)
        self._results_table.setAlternatingRowColors(True)
        self._results_table.verticalHeader().hide()
        self._results_table.horizontalHeader().setStretchLastSection(True)
        self._results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        self._results_table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._results_table.doubleClicked.connect(self._on_result_double_click)

        self._results_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._results_table.customContextMenuRequested.connect(self._show_context_menu)

        results_layout.addWidget(self._results_table)
        right_splitter.addWidget(results_container)

        # ---- Preview Section ----
        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        playback_state = 'On' if config.SCENE_PLAYBACK else 'Off'
        self._playback_toggle_btn = QPushButton(f'Toggle preview playback ({playback_state})')
        self._playback_toggle_btn.clicked.connect(self.toggle_preview_playback)
        preview_layout.addWidget(self._playback_toggle_btn)

        self._preview_stack = QStackedWidget()

        # Page 0: Static image preview
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setFrameShape(QFrame.NoFrame)
        self._preview_image_label = QLabel()
        self._preview_image_label.setAlignment(Qt.AlignCenter)
        self._preview_image_label.setStyleSheet("background-color: #555;")
        self._preview_scroll.setWidget(self._preview_image_label)
        self._preview_stack.addWidget(self._preview_scroll)

        # Page 1: VLC video container
        self._video_container = QFrame()
        self._video_container.setStyleSheet("background-color: black;")
        self._preview_stack.addWidget(self._video_container)

        self._preview_stack.setCurrentIndex(0)
        self._preview_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ---- Horizontal Splitter: Preview | Thumbnails ----
        media_splitter = QSplitter(Qt.Horizontal)
        media_splitter.setHandleWidth(4)
        media_splitter.setStyleSheet("QSplitter::handle { background: rgba(128, 128, 128, 0.2); }")
        media_splitter.addWidget(self._preview_stack)

        self._thumb_list = QListWidget()
        self._thumb_list.setViewMode(QListWidget.IconMode)
        self._thumb_list.setResizeMode(QListWidget.Adjust)
        self._thumb_list.setFlow(QListWidget.LeftToRight)
        self._thumb_list.setWordWrap(True)
        self._thumb_list.setIconSize(QSize(120, 68))
        self._thumb_list.setSpacing(4)
        self._thumb_list.setFrameShape(QFrame.NoFrame)
        self._thumb_list.itemClicked.connect(self._on_thumbnail_click)
        self._thumb_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        media_splitter.addWidget(self._thumb_list)

        media_splitter.setSizes([400, 400])
        preview_layout.addWidget(media_splitter)

        self._export_btn = QPushButton('Export Scene...')
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self.open_export_dialog)
        preview_layout.addWidget(self._export_btn)

        right_splitter.addWidget(preview_container)
        right_splitter.setSizes([400, 400])

        layout.addWidget(right_splitter)

    # ======================================================================
    # Drop zone callback
    # ======================================================================

    def _on_dropzone_drop(self, paths):
        if not paths:
            self.browse_files_dialog()
            return
        self._add_paths_to_queue(paths)

    # ======================================================================
    # Database section update
    # ======================================================================

    def _update_db_section(self):
        if self.primary_db:
            target_name = os.path.basename(self.primary_db)
            self._db_target_label.setText(f'\u2605 {target_name}')
            self._statusbar_db_label.setText(f'DB: {target_name}')
        else:
            self._db_target_label.setText('No target database set')
            self._statusbar_db_label.setText('DB: none')

        search_count = len(self.active_databases)
        if search_count > 0:
            extra = search_count - 1 if self.primary_db in self.active_databases else search_count
            if extra > 0:
                self._db_search_label.setText(f'+ {extra} additional search database(s)')
            else:
                self._db_search_label.setText('')
        else:
            self._db_search_label.setText('')

    # ======================================================================
    # Worker thread management
    # ======================================================================

    def _connect_worker(self, worker, bridge: SignalBridge):
        from PySide6.QtCore import Qt as QtCoreEnum
        q = QtCoreEnum.QueuedConnection
        worker.signals.status_updated.connect(bridge.relay_status, type=q)
        worker.signals.progress_updated.connect(bridge.relay_progress, type=q)
        worker.signals.finished.connect(bridge.relay_finished, type=q)
        worker.signals.error.connect(bridge.relay_error, type=q)

    def _run_worker(self, worker, bridge: SignalBridge):
        if not getattr(self, 'is_active', True):
            return
        self._connect_worker(worker, bridge)
        self._current_worker = worker
        worker.start()

    # ======================================================================
    # Status updates
    # ======================================================================

    @Slot(str)
    def update_status(self, message: str):
        if hasattr(self, '_status_label') and self._status_label:
            self._status_label.setText(message)
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(message, 5000)

    def force_update_status(self, message: str):
        self.update_status(message)
        QApplication.processEvents()

    # ======================================================================
    # Config / database persistence
    # ======================================================================

    def save_config_key(self, key, value):
        self.config[key] = value
        config.save_config(self.config)

    def save_trt_preference(self, checked: bool):
        self.config['use_trt'] = checked
        config.save_config(self.config)

    def save_db_config(self):
        self.config['active_databases'] = self.active_databases
        self.config['primary_database'] = self.primary_db if self.primary_db else ''
        config.save_config(self.config)

    def _on_device_changed(self, device: str):
        self.device_choice = device
        self.save_config_key('device', device)
        self._update_standby_ui_state()

    def _on_detect_method_changed(self, fast_mode: bool):
        self.save_config_key('fast_detect', fast_mode)

    def _add_databases(self, paths):
        added = []
        for path in paths:
            abs_path = str(Path(path).resolve())
            if abs_path not in self.active_databases:
                self.active_databases.append(abs_path)
                init_db(abs_path, self.handle_migration_callback)
                added.append(abs_path)
        if added:
            if not self.primary_db:
                self.primary_db = added[0]
            self._update_db_section()
            self.save_db_config()
            self._update_button_states()
            self.update_status(f'Added {len(added)} database(s).')
            if self.db_manager_dlg and hasattr(self.db_manager_dlg, 'isVisible') and self.db_manager_dlg.isVisible():
                self.db_manager_dlg.refresh()
        self.verify_database_paths()

    def load_saved_paths(self):
        from database import queue_count

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
            init_db(db_path, self.handle_migration_callback)

        self._update_db_section()
        self.update_queue_status()
        self._update_button_states()
        self.verify_database_paths()

    def update_queue_status(self):
        from database import queue_count
        if not self.primary_db:
            status_text = '[0] items in queue (no target database)'
            if hasattr(self, '_queue_status_label') and self._queue_status_label:
                self._queue_status_label.setText(status_text)
            self._index_button.setEnabled(False)
            return
        count = queue_count(self.primary_db)
        status_text = f'[{count}] items in queue'
        if hasattr(self, '_queue_status_label') and self._queue_status_label:
            self._queue_status_label.setText(status_text)
        self._index_button.setEnabled(count > 0)

    def _add_paths_to_queue(self, paths):
        if not self.primary_db:
            QMessageBox.critical(self, 'Error', 'Please select a target database first.')
            return
        from database import add_to_queue
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
            if self.queue_manager_dlg and hasattr(self.queue_manager_dlg, 'isVisible') and self.queue_manager_dlg.isVisible():
                self.queue_manager_dlg.refresh()
        elif paths:
            self.update_status('No valid media files or directories dropped.')

    # ======================================================================
    # Button state management
    # ======================================================================

    def set_controls_enabled(self, enabled: bool):
        if hasattr(self, '_load_model_button'):
            self._load_model_button.setEnabled(enabled)
        if hasattr(self, '_device_combobox'):
            self._device_combobox.setEnabled(enabled)
        if hasattr(self, '_theme_combobox'):
            self._theme_combobox.setEnabled(enabled)
        if hasattr(self, '_standby_check'):
            self._standby_check.setEnabled(enabled and self.device_choice != 'cpu')
        self._update_button_states()

    def _update_button_states(self):
        has_search_dbs = len(self.active_databases) > 0
        has_target = self.primary_db is not None
        model_loaded = self.model is not None

        if hasattr(self, '_search_button') and self._search_button:
            self._search_button.setEnabled(has_search_dbs and model_loaded)
        if hasattr(self, '_index_button') and self._index_button:
            self._index_button.setEnabled(has_target)
        if hasattr(self, '_query_text_edit') and self._query_text_edit:
            self._query_text_edit.setEnabled(has_search_dbs and model_loaded)

    # ======================================================================
    # Model loading
    # ======================================================================

    def load_model(self, device_choice=None, use_trt=None, status_callback=None):
        if device_choice is None:
            device_choice = self.device_choice
        if use_trt is None:
            use_trt = self.config.get('use_trt', False)
        if status_callback is None:
            status_callback = self.update_status

        self.model, self.processor, self.device, self.dtype, self._last_active_device = load_siglip_model(
            device_choice,
            status_callback=status_callback,
            use_trt=use_trt,
        )

    def threaded_load_model(self):
        self.update_status(f'Loading model: {config.DEFAULT_MODEL}...')

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            status=self.update_status,
            finished=lambda result: self._on_model_worker_done(result),
            error=lambda msg: (
                QMessageBox.critical(self, 'Model Error', msg),
                self.update_status('Error loading model.'),
            ),
        )
        worker = ModelLoadWorker(self.device_choice, self.config.get('use_trt', False))
        self._run_worker(worker, bridge)

    def _on_model_worker_done(self, result):
        model, processor, device, dtype, last_active = result
        self.model = model
        self.processor = processor
        self.device = device
        self._last_active_device = last_active
        self.on_model_load_finished()

    def on_model_load_finished(self):
        self._is_background_task_running = False
        if hasattr(self, '_last_active_device'):
            self.device_choice = self._last_active_device
        self.update_status(f'Model loaded on {str(self.device).upper()}. Ready!')
        self.set_controls_enabled(True)
        if hasattr(self, '_load_model_button') and self._load_model_button:
            self._load_model_button.setText('Reload Model')

    # ======================================================================
    # Indexing
    # ======================================================================

    def threaded_index(self):
        if not self.primary_db:
            QMessageBox.critical(self, 'Error', 'Please select a target database first.')
            return
        from database import queue_count
        if queue_count(self.primary_db) == 0:
            QMessageBox.critical(self, 'Error', 'Please add files or folders to the queue before indexing.')
            return
        self._is_background_task_running = True
        self.ensure_model_active()
        self._stop_video_loop()

        self._index_dialog = IndexProgressDialog(self)
        self._index_dialog.cancel_btn.clicked.connect(self.cancel_indexing)
        self._index_dialog.show()

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            progress=self._update_index_progress,
            finished=lambda result: self._on_index_finished(result),
            error=lambda msg: (
                self._index_dialog.close(),
                QMessageBox.critical(self, 'Indexing Error', msg),
                print('Indexing Error', msg),
            ),
        )
        worker = IndexWorker(
            self.device, self.processor, self.model, self.primary_db,
            batch_size=self.config.get('batch_size', 16),
            generate_thumbnails=self.config.get('generate_thumbnails', True),
            max_num_patches=self.config.get('max_patches', 256),
            fast_scene_detect=self.config.get('fast_detect', True),
            frames_per_scene=self.config.get('frames_per_scene', 3),
            force_reprocess=self.config.get('force_reprocess', False),
        )
        self._index_worker = worker
        self._run_worker(worker, bridge)
        self.update_status('Indexing in progress...')

    def cancel_indexing(self):
        if hasattr(self, '_index_worker') and self._index_worker and self._index_worker.isRunning():
            self._index_worker.requestInterruption()
        if hasattr(self, '_index_dialog') and self._index_dialog:
            self._index_dialog.status_label.setText('Cancelling...')
            self._index_dialog.cancel_btn.setEnabled(False)

    def _update_index_progress(self, data):
        if not hasattr(self, '_index_dialog') or not self._index_dialog:
            return
        if isinstance(data, dict):
            curr = data.get('current', 0)
            total = data.get('total', 0)
            fname = data.get('file', 'Initializing...')
            self._index_dialog.progress_bar.setMaximum(total)
            self._index_dialog.progress_bar.setValue(curr)
            self._index_dialog.file_label.setText(f'Current file: {fname}')
            self._index_dialog.count_label.setText(f'{curr} / {total}')
            if curr > 0:
                self._index_dialog.status_label.setText('Processing Media...')
        elif isinstance(data, str):
            self._index_dialog.status_label.setText(data)

    def _on_index_finished(self, result: str = 'completed'):
        self._is_background_task_running = False
        if hasattr(self, '_index_dialog') and self._index_dialog:
            self._index_dialog.close()
            self._index_dialog = None
        if result == 'cancelled':
            self.update_status('Indexing cancelled.')
            QMessageBox.information(self, 'Cancelled', 'Indexing was cancelled.')
        else:
            if self.primary_db:
                from database import clear_queue
                clear_queue(self.primary_db)
                self.update_queue_status()
            self.update_status('Indexing complete!')
            QMessageBox.information(self, 'Complete', 'Indexing has finished.')
        self._update_button_states()

    # ======================================================================
    # Search
    # ======================================================================

    def threaded_search(self):
        if not self.active_databases:
            QMessageBox.critical(self, 'Error', 'Please add at least one database to search.')
            return
        assert self.active_databases, "No active databases"
        if db_is_empty(self.active_databases[0]):
            all_empty = all(db_is_empty(db) for db in self.active_databases)
            if all_empty:
                QMessageBox.warning(self, 'Warning', 'All active databases appear to be empty. Please index files before searching.')
                return
        query_text = ''
        if hasattr(self, '_query_text_edit') and self._query_text_edit:
            query_text = self._query_text_edit.text()
        if not query_text and (not self.query_image_path):
            QMessageBox.warning(self, 'Warning', 'Please enter text or select an image to search.')
            return
        self._is_background_task_running = True
        self.ensure_model_active()
        self._stop_video_loop()
        self.update_status('Searching...')

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            finished=lambda results: self._on_search_finished(results),
            error=lambda msg: (
                QMessageBox.critical(self, 'Search Error', msg),
                print('Search Error', msg),
            ),
        )
        worker = SearchWorker(
            query_text, self.query_image_path,
            self.device, self.processor, self.model,
            self.active_databases, self.config.get('top_k', 20), self.config.get('max_patches', 256),
        )
        self._index_worker = worker
        self._run_worker(worker, bridge)

    def _on_search_finished(self, results):
        self._is_background_task_running = False
        self.search_results = [
            (path, score, 'video', None, scene_idx, start_time, end_time, thumb_bytes, source_db)
            for path, scene_idx, start_time, end_time, thumb_bytes, score, source_db in results
        ]
        self._update_listview()
        self.update_status(f'Found {len(results)} results.')
        self._rescore_button.setEnabled(len(results) > 0)

    # ======================================================================
    # Rescore
    # ======================================================================

    def open_rescore_dialog(self):
        query_text, ok = QInputDialog.getText(self, 'Rescore', 'Enter new text query to rescore results:')
        if not ok or not query_text:
            return
        assert self.primary_db is not None
        self._is_background_task_running = True
        self.update_status(f"Rescoring with: '{query_text}'...")

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            finished=lambda results: self._on_rescore_finished(results),
            error=lambda msg: (
                QMessageBox.critical(self, 'Rescore Error', msg),
            ),
        )
        worker = RescoreWorker(
            self.search_results, query_text, self.primary_db,
            self.device, self.processor, self.model,
        )
        self._run_worker(worker, bridge)

    def _on_rescore_finished(self, updated_results):
        self._is_background_task_running = False
        self.search_results = updated_results
        self._update_listview()
        self.update_status('Rescore complete.')
        self._clear_rescore_button.setEnabled(True)

    def clear_rescore(self):
        self.search_results = [
            (path, score, ftype, None, scene_idx, scene_time, scene_end, thumb_bytes, source_db)
            for path, score, ftype, _, scene_idx, scene_time, scene_end, thumb_bytes, source_db
            in self.search_results
        ]
        self._update_listview()
        self.update_status('Rescore cleared.')
        self._clear_rescore_button.setEnabled(False)

    # ======================================================================
    # File dialogs
    # ======================================================================

    def browse_database(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Create New Database File', '',
            'SQLite Database (*.db)',
        )
        if path:
            if not path.endswith('.db'):
                path += '.db'
            abs_path = str(Path(path).resolve())
            init_db(abs_path, self.force_update_status)
            self.active_databases.append(abs_path)
            self.primary_db = abs_path
            self._update_db_section()
            self.save_db_config()
            self._update_button_states()
            self.update_queue_status()
            self.update_status(f'Database created and set as target: {os.path.basename(path)}')

    def browse_existing_database(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Select Existing Database Files', '',
            'SQLite Database (*.db)',
        )
        if paths:
            self._add_databases(paths)

    def browse_query_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Query Image', '',
            'Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)',
        )
        if path:
            self.query_image_path = path
            if hasattr(self, '_query_image_label') and self._query_image_label:
                self._query_image_label.setText(os.path.basename(path))

    def clear_query_image(self):
        self.query_image_path = None
        if hasattr(self, '_query_image_label') and self._query_image_label:
            self._query_image_label.setText('No query image')

    def browse_files_dialog(self):
        if not self.primary_db:
            QMessageBox.critical(self, 'Error', 'Please select a target database first.')
            return
        all_exts = list(config.IMAGE_EXTENSIONS) + list(config.VIDEO_EXTENSIONS)
        filter_str = 'Media Files ('
        filter_str += ' '.join(f'*{e}' for e in all_exts)
        filter_str += ')'
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Select Media Files', '', filter_str,
        )
        if paths:
            self._add_paths_to_queue(paths)

    def add_folder_to_queue(self):
        if not self.primary_db:
            QMessageBox.critical(self, 'Error', 'Please select a target database first.')
            return
        path = QFileDialog.getExistingDirectory(self, 'Select Folder to Add to Queue')
        if path:
            self._add_paths_to_queue([path])

    def add_files_to_queue(self):
        self.browse_files_dialog()

    # ======================================================================
    # Database verification
    # ======================================================================

    def verify_database_paths(self):
        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            finished=lambda missing: (
                self.show_missing_files_dialog(missing) if missing else None
            ),
        )
        worker = VerifyPathsWorker(self.active_databases)
        self._run_worker(worker, bridge)

    def show_missing_files_dialog(self, missing_files: list):
        if not missing_files:
            return

        from database import update_video_filepath, delete_video_record

        dlg = QDialog(self)
        dlg.setWindowTitle(f'Missing Files ({len(missing_files)})')
        dlg.setMinimumSize(700, 450)
        dlg.resize(700, 500)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)

        header = QLabel(f'Found {len(missing_files)} missing video file(s).\n'
                        'They may have been moved, renamed, or deleted.')
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(4)

        remaining = list(missing_files)

        def refresh_count():
            count_label.setText(
                f'{len(remaining)} missing entr{"y" if len(remaining) == 1 else "ies"} remaining'
                if remaining else 'No missing entries remaining.'
            )
            if not remaining:
                header.setText('All missing files have been resolved!')

        def remove_row(frame, db_path, video_id):
            scroll_layout.removeWidget(frame)
            frame.deleteLater()
            for i, (rd, rv, _) in enumerate(remaining):
                if rd == db_path and rv == video_id:
                    remaining.pop(i)
                    break
            refresh_count()

        all_exts = config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS
        filter_str = 'Media Files (' + ' '.join(f'*{e}' for e in all_exts) + ')'

        for db_path, video_id, filepath in missing_files:
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            row = QHBoxLayout(frame)
            row.setContentsMargins(8, 6, 8, 6)
            row.setSpacing(8)

            info = QLabel(f'{os.path.basename(filepath)}\n{db_path}')
            info.setWordWrap(True)
            row.addWidget(info, stretch=1)

            find_btn = QPushButton('Find...')
            find_btn.clicked.connect(
                lambda checked, d=db_path, v=video_id, f=filepath: (
                    self._resolve_missing_find(dlg, d, v, f, filter_str,
                                               lambda: remove_row(frame, d, v))
                )
            )
            row.addWidget(find_btn)

            remove_btn = QPushButton('Remove')
            remove_btn.clicked.connect(
                lambda checked, d=db_path, v=video_id, f=filepath: (
                    self._resolve_missing_remove(dlg, d, v, f,
                                                 lambda: remove_row(frame, d, v))
                )
            )
            row.addWidget(remove_btn)

            scroll_layout.addWidget(frame)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, stretch=1)

        count_label = QLabel()
        layout.addWidget(count_label)
        refresh_count()

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        dlg.exec()

    def _resolve_missing_find(self, dlg, db_path, video_id, filepath,
                               filter_str, on_success):
        from database import update_video_filepath
        new_path, _ = QFileDialog.getOpenFileName(
            dlg, f'Locate: {os.path.basename(filepath)}', '',
            f'{filter_str};;All Files (*)',
        )
        if new_path:
            if update_video_filepath(db_path, video_id, new_path):
                on_success()
            else:
                QMessageBox.warning(
                    dlg, 'Error',
                    'Could not update the path. The new path may already exist '
                    'in the database.'
                )

    def _resolve_missing_remove(self, dlg, db_path, video_id, filepath,
                                 on_success):
        from database import delete_video_record
        reply = QMessageBox.question(
            dlg, 'Confirm Remove',
            f'Remove this entry and all associated scene embeddings?\n'
            f'{os.path.basename(filepath)}',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            delete_video_record(db_path, video_id)
            on_success()

    # ======================================================================
    # Database combine
    # ======================================================================

    def _combine_task(self, out_path: str):
        self.set_controls_enabled(False)
        self.show_merging_popup()

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            status=self.update_status,
            finished=lambda result: (
                self.close_merging_popup(),
                self.set_controls_enabled(True),
                self._add_databases([result]),
                self.update_status('Database merge complete.'),
                QMessageBox.information(self, 'Success', 'Databases combined successfully.'),
            ),
            error=lambda msg: (
                self.close_merging_popup(),
                self.set_controls_enabled(True),
                QMessageBox.critical(self, 'Merge Error', f'Failed to combine databases: {msg}'),
            ),
        )
        worker = CombineDBWorker(self.active_databases, out_path)
        self._run_worker(worker, bridge)

    # ======================================================================
    # Migration / merge popups 
    # ======================================================================

    def show_migration_popup(self):
        dlg = QProgressDialog('Upgrading database schema...', None, 0, 0, self)
        dlg.setWindowTitle('Database Migration')
        dlg.setModal(True)
        dlg.setMinimumWidth(350)
        self._migration_progress = dlg
        dlg.show()

    def close_migration_popup(self):
        if hasattr(self, '_migration_progress') and self._migration_progress:
            self._migration_progress.close()
            self._migration_progress = None

    def handle_migration_callback(self, msg: str):
        if hasattr(self, '_migration_progress') and self._migration_progress:
            self._migration_progress.setLabelText(msg)
        self.force_update_status(msg)

    def show_merging_popup(self):
        dlg = QProgressDialog('Merging databases...', None, 0, 0, self)
        dlg.setWindowTitle('Merging Databases')
        dlg.setModal(True)
        dlg.setMinimumWidth(350)
        self._merge_progress = dlg
        dlg.show()

    def close_merging_popup(self):
        if hasattr(self, '_merge_progress') and self._merge_progress:
            self._merge_progress.close()
            self._merge_progress = None

    def update_merge_status(self, message: str):
        if hasattr(self, '_merge_progress') and self._merge_progress:
            self._merge_progress.setLabelText(message)
        self.update_status(message)

    # ======================================================================
    # GPU standby
    # ======================================================================

    def toggle_model_standby(self, to_cpu: bool):
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

    def reset_idle_timer(self):
        self._idle_counter = 0
        if self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)

    def check_idle_and_state(self):
        if not self.is_active or self._is_background_task_running:
            self._idle_counter = 0
            return
        self._idle_counter += 1
        idle_limit = self.config.get('idle_offload_seconds', 300)
        if self.config.get('gpu_standby', True) and self._idle_counter >= idle_limit:
            if not self._is_in_standby:
                self.toggle_model_standby(to_cpu=True)
        self._idle_timer = QTimer.singleShot(1000, self.check_idle_and_state)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                if hasattr(self, 'player') and self.player.is_playing():
                    vlc_time = self.player.get_time()
                    if vlc_time > 0:
                        self._resume_time_ms = vlc_time
                    else:
                        self._resume_time_ms = None
                    self._stop_video_loop()
                else:
                    self._resume_time_ms = None

                if self.config.get('gpu_standby', True):
                    self.toggle_model_standby(to_cpu=True)

            elif event.oldState() & Qt.WindowMinimized:
                self.ensure_model_active()

                if config.SCENE_PLAYBACK:
                    self.last_selected_entry = None
                    QTimer.singleShot(100, self._on_selection_changed)
                    self._on_selection_changed()

        super().changeEvent(event)

    def ensure_model_active(self):
        if self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)
        self._idle_counter = 0

    def _update_standby_ui_state(self):
        if hasattr(self, '_standby_check') and self._standby_check:
            self._standby_check.setEnabled(self.device_choice != 'cpu')

    def _on_standby_toggle_changed(self, checked: bool):
        self.save_config_key('gpu_standby', checked)
        if not checked and self._is_in_standby:
            self.toggle_model_standby(to_cpu=False)

    # ======================================================================
    # Search Results — List View
    # ======================================================================

    def _format_ms(self, ms: int) -> str:
        hours = ms // 3600000
        mins = (ms % 3600000) // 60000
        secs = (ms % 60000) // 1000
        milli = ms % 1000
        if hours > 0:
            return f"{hours}:{mins:02d}:{secs:02d}.{milli:03d}"
        return f"{mins}:{secs:02d}.{milli:03d}"

    def _update_listview(self, preserve_sort=False):
        # Clear thumbnails
        self._clear_thumbnails()

        if not self.search_results:
            self._stats_label.setText('No results found.')
            self.results_model.update_data([])
            return

        self.last_selected_entry = None
        has_rescore = self.search_results and self.search_results[0][3] is not None

        if not preserve_sort:
            self.current_sort_col = 'rescore' if has_rescore else 'score'
            self.current_sort_reverse = True
            sort_key = lambda x: x[3] if has_rescore else x[1]
            self.search_results.sort(key=sort_key, reverse=True)

        self._thumb_list.setUpdatesEnabled(False)

        display_data = []
        for i, data in enumerate(self.search_results):
            path, score, ftype, rescore, scene_idx, scene_time, scene_end, thumb_bytes, source_db = data

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

            score_str = f'{score:.4f}'
            rescore_str = f'{rescore:.4f}' if rescore is not None else ''

            display_data.append([filename, scene_str, time_str, source_db, score_str, rescore_str])

            if thumb_bytes:
                pixmap = QPixmap()
                pixmap.loadFromData(thumb_bytes)
                item = QListWidgetItem()
                item.setIcon(QIcon(pixmap))
                item.setData(Qt.UserRole, i)
                item.setSizeHint(QSize(120, 68))
                self._thumb_list.addItem(item)
                self._thumbnail_refs.append(item)

        self._thumb_list.setUpdatesEnabled(True)

        self.results_model.update_data(display_data)

        # Auto-sort by Score column descending
        self._results_table.sortByColumn(4, Qt.DescendingOrder)

        # Widen Time column to fit timestamp ranges
        header = self._results_table.horizontalHeader()
        header.resizeSection(2, header.sectionSizeHint(2) * 2)

        # Stats
        scores = [
            rescore if has_rescore and rescore is not None else score
            for _, score, _, rescore, _, _, _, _, _ in self.search_results
        ]
        stats_text = f'Found {len(scores)} results | Max: {max(scores):.3f} | Avg: {np.mean(scores):.3f}'
        self._stats_label.setText(stats_text)
        self._statusbar_count_label.setText(f'{len(scores)} results')

        # Auto-select first result
        if self.results_model.rowCount() > 0:
            first_index = self.results_proxy.index(0, 0)
            self._results_table.selectionModel().select(
                first_index,
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )
            self._on_selection_changed()

    def _clear_thumbnails(self):
        self._thumbnail_refs = []
        self._thumb_list.clear()

    # ======================================================================
    # Thumbnail interaction
    # ======================================================================

    def _on_thumbnail_click(self, item):
        scene_index = item.data(Qt.UserRole)
        if scene_index is None:
            return
        source_index = self.results_model.index(scene_index, 0)
        proxy_index = self.results_proxy.mapFromSource(source_index)
        if proxy_index.isValid():
            self._results_table.selectionModel().select(
                proxy_index,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
            )
            self._results_table.scrollTo(proxy_index)
            self._on_selection_changed()

    def _scroll_thumb_to_view(self, index: int):
        if index < len(self._thumbnail_refs):
            item = self._thumbnail_refs[index]
            self._thumb_list.scrollToItem(item)

    # ======================================================================
    # Table selection
    # ======================================================================

    def _on_selection_changed(self):
        sel_model = self._results_table.selectionModel()
        indexes = sel_model.selectedRows()
        if not indexes:
            self._export_btn.setEnabled(False)
            self._export_btn.setText('Export Scene...')
            return

        idx = indexes[0]
        source_row = self.results_proxy.mapToSource(idx).row()
        if source_row >= len(self.search_results):
            return

        if len(indexes) > 1:
            exportable_count = 0
            for idx in indexes:
                source_row = self.results_proxy.mapToSource(idx).row()
                if source_row < len(self.search_results):
                    if self.search_results[source_row][2] == 'video' and self.search_results[source_row][5] is not None:
                        exportable_count += 1
            if exportable_count > 0:
                self._export_btn.setEnabled(True)
                self._export_btn.setText(f'Export {exportable_count} Scenes...')
            else:
                self._export_btn.setEnabled(False)
                self._export_btn.setText('Export Scene...')
        else:
            path, _, ftype, _, _, start_ms, end_ms, _, _ = self.search_results[source_row]
            is_video = (ftype == 'video')
            can_export = is_video and start_ms is not None
            self._export_btn.setEnabled(can_export)
            self._export_btn.setText('Export Scene...')

        if source_row != self.last_selected_entry:
            self.last_selected_entry = source_row
            path, _, ftype, _, _, start_ms, end_ms, _, _ = self.search_results[source_row]
            self.display_media(path, is_video=(ftype == 'video'), start_ms=start_ms, end_ms=end_ms)

        # Highlight thumbnail in list
        for ref in self._thumbnail_refs:
            if ref.data(Qt.UserRole) == source_row:
                self._thumb_list.setCurrentItem(ref)
                break

        self._scroll_thumb_to_view(source_row)

    def _on_result_double_click(self, index):
        source_row = self.results_proxy.mapToSource(index).row()
        if source_row >= len(self.search_results):
            return
        path = self.search_results[source_row][0]
        self.current_display_path = path
        self.open_current_file()

    # ======================================================================
    # Context Menu
    # ======================================================================

    def _show_context_menu(self, position):
        indexes = self._results_table.selectionModel().selectedRows()
        if not indexes:
            return

        menu = QMenu(self._results_table)

        exportable = []
        for idx in indexes:
            row = self.results_proxy.mapToSource(idx).row()
            if row < len(self.search_results):
                path, _, ftype, _, _, start_ms, end_ms, _, _ = self.search_results[row]
                if ftype == 'video' and start_ms is not None:
                    exportable.append(row)

        if exportable:
            label = "Export Scene..." if len(exportable) == 1 else f"Export {len(exportable)} Scenes..."
            export_action = QAction(label, self)
            export_action.triggered.connect(self.open_export_dialog)
            menu.addAction(export_action)
            menu.addSeparator()

        if len(indexes) == 1:
            row = self.results_proxy.mapToSource(indexes[0]).row()
            path = self.search_results[row][0]

            open_action = QAction('Open File', self)
            open_action.triggered.connect(self.open_current_file)
            menu.addAction(open_action)

            copy_action = QAction('Copy Path', self)
            copy_action.triggered.connect(lambda: self._copy_path(path))
            menu.addAction(copy_action)

            folder_action = QAction('Open Containing Folder', self)
            folder_action.triggered.connect(self.open_containing_folder)
            menu.addAction(folder_action)

            menu.addSeparator()
            similar_action = QAction('Search for Similar', self)
            similar_action.triggered.connect(self.search_for_similar_preview_frame)
            menu.addAction(similar_action)
        else:
            copy_multi_action = QAction(f'Copy {len(indexes)} Paths', self)
            copy_multi_action.triggered.connect(self._copy_multiple_paths)
            menu.addAction(copy_multi_action)

        menu.exec(self._results_table.viewport().mapToGlobal(position))

    def _copy_path(self, path: str):
        clipboard = QApplication.clipboard()
        clipboard.setText(path)

    def _copy_multiple_paths(self):
        paths = []
        for idx in self._results_table.selectionModel().selectedRows():
            row = self.results_proxy.mapToSource(idx).row()
            if row < len(self.search_results):
                paths.append(self.search_results[row][0])
        if paths:
            clipboard = QApplication.clipboard()
            clipboard.setText('\n'.join(paths))

    # ======================================================================
    # Preview / Media Display
    # ======================================================================

    def display_media(self, path: str, is_video: bool, start_ms: int = 0, end_ms: int = 0):
        self._stop_video_loop()
        self.original_image = None
        self.current_display_path = path
        self.current_end_ms = end_ms

        if not is_video:
            self._preview_stack.setCurrentIndex(0)
            self._show_static_pil_image(path)
            return

        if not config.SCENE_PLAYBACK:
            self._preview_stack.setCurrentIndex(0)
            self._extract_and_show_first_frame(path, start_ms)
        else:
            self._preview_stack.setCurrentIndex(1)
            QTimer.singleShot(50, lambda: self._start_vlc_playback(path, start_ms, end_ms))

    def _show_static_pil_image(self, path):
        try:
            img = Image.open(path).convert('RGB')
            self._set_preview_pixmap(img)
        except Exception as e:
            self.update_status(f"Preview Error: {e}")

    def _set_preview_pixmap(self, pil_img):
        self.original_image = pil_img
        # Scale to fit the scroll area
        max_w = self._preview_scroll.viewport().width() - 10
        max_h = self._preview_scroll.viewport().height() - 10
        if max_w < 10 or max_h < 10:
            max_w, max_h = 400, 300

        img_w, img_h = pil_img.size
        scale = min(max_w / img_w, max_h / img_h, 1.0)
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)

        if new_w < 1 or new_h < 1:
            self._preview_image_label.clear()
            return

        resized = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format='PNG')
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        self._preview_image_label.setPixmap(pixmap)

    # ======================================================================
    # VLC Playback
    # ======================================================================

    def _set_vlc_window_handle(self, window_id):
        if sys.platform == 'win32':
            self.player.set_hwnd(window_id)
        elif sys.platform == 'darwin':
            self.player.set_nsobject(window_id)
        else:
            self.player.set_xwindow(window_id)

    def _start_vlc_playback(self, path, start_ms, end_ms):
        try:
            media = self.vlc_instance.media_new(path)
            playback_margin_ms = 100

            safe_start = int(start_ms) if isinstance(start_ms, (int, float)) else 0
            safe_end = int(end_ms) if isinstance(end_ms, (int, float)) else 0

            if safe_start > 0:
                media.add_option(f'start-time={safe_start / 1000.0}')
            if safe_end > 0:
                safe_end_ms = max(safe_start + 50, safe_end - playback_margin_ms)
                media.add_option(f'stop-time={safe_end_ms / 1000.0}')

            media.add_option('input-repeat=65535')

            self.current_media = media
            self.player.set_media(media)

            window_id = int(self._video_container.winId())
            self._set_vlc_window_handle(window_id)
            self.player.play()
        except Exception as e:
            self.update_status(f"VLC Error: {e}")

    def _stop_video_loop(self):
        if hasattr(self, 'player'):
            self.player.stop()
            if sys.platform == 'darwin':
                self.player.set_media(None)

    def toggle_preview_playback(self):
        config.SCENE_PLAYBACK = not config.SCENE_PLAYBACK
        self.config['scene_playback'] = config.SCENE_PLAYBACK
        config.save_config(self.config)

        state = 'On' if config.SCENE_PLAYBACK else 'Off'
        self._playback_toggle_btn.setText(f'Toggle preview playback ({state})')

        if not config.SCENE_PLAYBACK:
            self._stop_video_loop()
            self._preview_stack.setCurrentIndex(0)

        self.last_selected_entry = None
        self._on_selection_changed()

    # ======================================================================
    # Frame extraction for non-playback preview
    # ======================================================================

    def _extract_and_show_first_frame(self, path, start_ms):
        container = None
        try:
            import av
            av.logging.set_level(av.logging.PANIC)
            container = av.open(path)
            stream = container.streams.video[0]
            target_pts = int((start_ms / 1000.0) / float(stream.time_base))
            container.seek(target_pts, stream=stream, any_frame=False, backward=True)
            for frame in container.decode(stream):
                current_frame_ms = int(frame.time * 1000)
                if current_frame_ms >= start_ms:
                    img = frame.to_image()
                    self._set_preview_pixmap(img)
                    return
        except Exception as e:
            self.update_status(f"Preview Error: {e}")
        finally:
            if container:
                container.close()

    # ======================================================================
    # Export
    # ======================================================================

    def open_export_dialog(self):
        sel_model = self._results_table.selectionModel()
        indexes = sel_model.selectedRows()
        if not indexes:
            return

        if len(indexes) > 1:
            scenes = []
            for idx in indexes:
                row = self.results_proxy.mapToSource(idx).row()
                if row < len(self.search_results):
                    scene_data = self.search_results[row]
                    if scene_data[2] == 'video' and scene_data[5] is not None and scene_data[6] is not None:
                        scenes.append(scene_data)
            if scenes:
                self._stop_video_loop()
                from exporters.bulk_exporter import BulkExportDialog
                dialog = BulkExportDialog(self, scenes)
                dialog.exec()
            return

        row = self.results_proxy.mapToSource(indexes[0]).row()
        self.open_export_dialog_for_index(row)

    def open_export_dialog_for_index(self, index: int):
        if index >= len(self.search_results):
            return

        path = self.search_results[index][0]
        start_ms = self.search_results[index][5]
        end_ms = self.search_results[index][6]
        original_playback_state = config.SCENE_PLAYBACK

        if original_playback_state:
            config.SCENE_PLAYBACK = False
            self.display_media(path, is_video=True, start_ms=start_ms, end_ms=end_ms)
        else:
            self._stop_video_loop()

        from exporters.single_exporter import SingleExportDialog
        dialog = SingleExportDialog(self, path, start_ms, end_ms)
        dialog.exec()

        if original_playback_state:
            config.SCENE_PLAYBACK = True
            sel = self._results_table.selectionModel().selectedRows()
            if sel:
                first_row = self.results_proxy.mapToSource(sel[0]).row()
                if first_row == index:
                    self.display_media(path, is_video=True, start_ms=start_ms, end_ms=end_ms)

    # ======================================================================
    # File operations
    # ======================================================================

    def open_current_file(self):
        if not self.current_display_path:
            return

        sel = self._results_table.selectionModel().selectedRows()
        start_ms = 0
        if sel:
            row = self.results_proxy.mapToSource(sel[0]).row()
            start_ms = self.search_results[row][5] or 0

        use_vlc_open = self.config.get('use_vlc_open', True)
        if use_vlc_open:
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
                else:
                    subprocess.Popen(["vlc", self.current_display_path] + vlc_flags)
                    return
            except Exception as e:
                print(f"Failed to open with VLC: {e}")

        # Fallback native opening
        try:
            if sys.platform == 'win32':
                os.startfile(self.current_display_path)
            elif sys.platform == 'darwin':
                subprocess.run(['open', self.current_display_path])
            else:
                subprocess.run(['xdg-open', self.current_display_path])
        except Exception as e:
            QMessageBox.critical(self, 'Error', f'Could not open file: {e}')

    def open_containing_folder(self):
        if self.current_display_path:
            folder = os.path.dirname(self.current_display_path)
            try:
                if sys.platform == 'win32':
                    subprocess.run(['explorer', '/select,', self.current_display_path])
                elif sys.platform == 'darwin':
                    subprocess.run(['open', '-R', self.current_display_path])
                else:
                    subprocess.run(['xdg-open', folder])
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Could not open folder: {e}')

    # ======================================================================
    # Search by preview frame
    # ======================================================================

    def search_for_similar_preview_frame(self):
        if not self.current_display_path:
            return
        if self.original_image:
            os.makedirs(config.TEMP_FOLDER, exist_ok=True)
            temp_path = os.path.abspath(os.path.join(config.TEMP_FOLDER, "temp_search_query.jpg"))
            try:
                self.original_image.save(temp_path, "JPEG", quality=95)
                self.query_image_path = temp_path
                if hasattr(self, '_query_image_label') and self._query_image_label:
                    self._query_image_label.setText(f"Frame from {os.path.basename(self.current_display_path)}")
                if hasattr(self, '_query_text_edit') and self._query_text_edit:
                    self._query_text_edit.clear()
                self.threaded_search()
            except Exception as e:
                QMessageBox.critical(self, "Search Error", f"Could not capture frame: {e}")
        else:
            if self.current_display_path.lower().endswith(config.IMAGE_EXTENSIONS):
                self.query_image_path = self.current_display_path
                if hasattr(self, '_query_image_label') and self._query_image_label:
                    self._query_image_label.setText(os.path.basename(self.current_display_path))
                if hasattr(self, '_query_text_edit') and self._query_text_edit:
                    self._query_text_edit.clear()
                self.threaded_search()

    # ======================================================================
    # Theme
    # ======================================================================

    def apply_theme(self):
        selected = self._theme_combobox.currentData()
        if not selected:
            return

        try:
            app = QApplication.instance()
            is_dark = 'dark' in selected.lower()

            if selected.endswith('.xml'):
                from qt_material import apply_stylesheet
                apply_stylesheet(app, theme=selected, extra={'density_scale': '0'})
            elif selected.endswith('.qss'):
                theme_path = config.THEMES_DIR / selected
                if theme_path.exists():
                    with open(theme_path, "r", encoding="utf-8") as f:
                        app.setStyleSheet(f.read())
                else:
                    default_theme = config.DEFAULT_CONFIG.get('theme', 'dark_lightgreen.xml')
                    QMessageBox.warning(self, 'Theme Missing', f'Could not find {selected}. Falling back to {default_theme}.')
                    idx = self._theme_combobox.findData(default_theme)
                    if idx >= 0:
                        self._theme_combobox.setCurrentIndex(idx)
                        self.apply_theme()
                    return

            self.save_config_key('theme', selected)
            self._video_container.setStyleSheet(
                'background-color: black;' if is_dark else 'background-color: #d3d3d3;'
            )
            text_color = '#e0e0e0' if is_dark else '#333333'
            self._drop_zone._label.setStyleSheet(f'color: {text_color};')
            display_name = selected.split('.')[0].replace('_', ' ').title()
            self.update_status(f'Theme set to {display_name}')
        except Exception as e:
            QMessageBox.warning(self, 'Theme Error', f'Failed to apply theme: {e}')

    # ======================================================================
    # Cleanup
    # ======================================================================

    def cleanup_database(self):
        if not self.primary_db:
            QMessageBox.critical(self, 'Error', 'Please select a target database first.')
            return
        reply = QMessageBox.question(
            self, 'Confirm',
            'Remove entries for deleted files from the database?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        bridge = self._active_bridge = SignalBridge()
        bridge.set_callbacks(
            status=self.update_status,
            finished=lambda count: (
                QMessageBox.information(self, 'Complete', f'Removed {count} orphaned embeddings.'),
                self.update_status('Cleanup complete.'),
            ),
            error=lambda msg: QMessageBox.critical(self, 'Cleanup Error', msg),
        )
        worker = CleanupWorker(self.primary_db)
        self._run_worker(worker, bridge)

    # ======================================================================
    # Dialog
    # ======================================================================

    def open_db_manager(self):
        from database import get_db_stats, get_all_processed_videos

        dlg = QDialog(self)
        dlg.setWindowTitle('Database Manager')
        dlg.resize(650, 450)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        layout = QVBoxLayout(dlg)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(['Database', 'Videos', 'Scenes'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(table)

        def refresh():
            table.setRowCount(0)
            for db_path in self.active_databases:
                row = table.rowCount()
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(db_path))
                try:
                    stats = get_db_stats(db_path)
                    v_count = stats.get('video_count', 0)
                    s_count = stats.get('scene_count', 0)
                    table.setItem(row, 1, QTableWidgetItem(str(v_count)))
                    table.setItem(row, 2, QTableWidgetItem(str(s_count)))
                except Exception:
                    table.setItem(row, 1, QTableWidgetItem('?'))
                    table.setItem(row, 2, QTableWidgetItem('?'))

        def add():
            paths, _ = QFileDialog.getOpenFileNames(
                dlg, 'Add Database Files', '',
                'SQLite Database (*.db)',
            )
            if paths:
                self._add_databases(paths)
                refresh()

        def remove_selected():
            rows = sorted(set(index.row() for index in table.selectedIndexes()), reverse=True)
            for row in rows:
                db_path = table.item(row, 0).text()
                if db_path in self.active_databases:
                    self.active_databases.remove(db_path)
                if db_path == self.primary_db:
                    self.primary_db = self.active_databases[0] if self.active_databases else None
            self._update_db_section()
            self.save_db_config()
            self._update_button_states()
            self.update_queue_status()
            refresh()

        btn_layout = QHBoxLayout()
        add_btn = QPushButton('Add Database')
        add_btn.clicked.connect(add)
        btn_layout.addWidget(add_btn)
        remove_btn = QPushButton('Remove Selected')
        remove_btn.clicked.connect(remove_selected)
        btn_layout.addWidget(remove_btn)
        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(refresh)
        btn_layout.addWidget(refresh_btn)
        btn_layout.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dlg.refresh = refresh
        refresh()
        self.db_manager_dlg = dlg
        dlg.exec()
        self.db_manager_dlg = None

    def open_queue_manager(self):
        from database import get_queue, remove_from_queue, clear_queue, queue_count

        dlg = QDialog(self)
        dlg.setWindowTitle('Queue Manager')
        dlg.resize(650, 450)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        layout = QVBoxLayout(dlg)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(['File', 'Type', 'Recursive'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(table)

        def refresh():
            table.setRowCount(0)
            if self.primary_db:
                items = get_queue(self.primary_db)
                from database import update_queue_recursive
                
                for item_id, path, is_directory, recursive in items:
                    row = table.rowCount()
                    table.insertRow(row)
                    table.setItem(row, 0, QTableWidgetItem(path))
                    table.setItem(row, 1, QTableWidgetItem('Folder' if is_directory else 'File'))
                    
                    if is_directory:
                        btn_text = 'Yes' if recursive else 'No'
                        toggle_btn = QPushButton(btn_text)
                        toggle_btn.setStyleSheet("font-weight: bold; color: #0078D7;")
                        
                        def toggle_func(checked, i=item_id, r=recursive):
                            new_val = not r
                            update_queue_recursive(self.primary_db, i, new_val)
                            refresh()
                            
                        toggle_btn.clicked.connect(toggle_func)
                        table.setCellWidget(row, 2, toggle_btn)
                    else:
                        table.setItem(row, 2, QTableWidgetItem('-'))
                    
                    table.item(row, 0).setData(Qt.UserRole, item_id)

        def remove_selected():
            rows = sorted(set(index.row() for index in table.selectedIndexes()), reverse=True)
            if not rows:
                return
            for row in rows:
                item_id = table.item(row, 0).data(Qt.UserRole)
                if item_id is not None:
                    remove_from_queue(self.primary_db, item_id)
            self.update_queue_status()
            refresh()

        def clear():
            reply = QMessageBox.question(
                dlg, 'Clear Queue',
                'Remove all items from the queue?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes and self.primary_db:
                clear_queue(self.primary_db)
                self.update_queue_status()
                refresh()

        btn_layout = QHBoxLayout()
        remove_btn = QPushButton('Remove Selected')
        remove_btn.clicked.connect(remove_selected)
        btn_layout.addWidget(remove_btn)
        clear_btn = QPushButton('Clear Queue')
        clear_btn.clicked.connect(clear)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dlg.refresh = refresh
        refresh()
        self.queue_manager_dlg = dlg
        dlg.exec()
        self.queue_manager_dlg = None

    def open_about_dialog(self):
        from gui_utils import center_window

        dlg = QDialog(self)
        dlg.setWindowTitle('About Scene Scout')
        dlg.setFixedSize(500, 300)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        main_layout = QHBoxLayout(dlg)
        main_layout.setContentsMargins(20, 20, 20, 20)

        logo_label = QLabel()
        try:
            pixmap = QPixmap(str(config.big_logo))
            scaled_logo = pixmap.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_label.setPixmap(scaled_logo)
        except Exception:
            logo_label.setText("Logo")
        logo_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        main_layout.addWidget(logo_label, stretch=1)

        info_layout = QVBoxLayout()

        header = QLabel('Scene Scout')
        header.setStyleSheet('font-size: 18px; font-weight: bold;')
        info_layout.addWidget(header)

        version_text = ''
        try:
            import toml
            pyproject_path = config.PROJECT_ROOT / 'pyproject.toml'
            if pyproject_path.exists():
                with open(pyproject_path) as f:
                    pyproject = toml.load(f)
                ver = pyproject.get('project', {}).get('version', '')
                if ver:
                    version_text = f'v{ver}'
        except Exception:
            pass

        if version_text:
            vlabel = QLabel(version_text)
            vlabel.setStyleSheet('font-size: 12px; color: gray;')
            info_layout.addWidget(vlabel)

        desc = QLabel(
            "Scene Scout is a tool written to help with searching for "
            "specific scenes using keywords. It was initially forked from"
            "Gabrjiele's project and uses Google's SigLIP 2 model "
            "for embedding and extracting visual information."
            "In the meantime it has been expanded to focus very specifically,"
            "to process, inspect and export specific scenes from videos."
        )
        desc.setWordWrap(True)
        info_layout.addWidget(desc)
        info_layout.addSpacing(10)

        grid = QGridLayout()
        links = [
            ('Logo by Miwo', 'https://4miwo.carrd.co'),
            ('GitHub Repo', 'https://github.com/Mark-Shun/scene-scout'),
            ('Codeberg Repo', 'https://codeberg.org/Mark-Shun/scene-scout'),
            ('Gitlab Repo', 'https://gitlab.com/Mark-Shun/scene-scout'),
            ('Initial fork', 'https://github.com/Gabrjiele/siglip2-naflex-search'),
        ]
        for i, (label, url) in enumerate(links):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, u=url: webbrowser.open_new(u))
            grid.addWidget(btn, i // 2, i % 2)

        info_layout.addLayout(grid)
        info_layout.addSpacing(8)

        copyright_label = QLabel('Developed by Sonicfreak1111/Mark-Shun \u00a9 2026')
        copyright_label.setStyleSheet('font-size: 10px; color: gray;')
        info_layout.addWidget(copyright_label)

        info_layout.addStretch()

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        info_layout.addWidget(close_btn, alignment=Qt.AlignRight)

        main_layout.addLayout(info_layout, stretch=3)

        center_window(dlg)
        dlg.exec()
