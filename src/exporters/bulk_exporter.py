import os
import sys
import subprocess

from PySide6.QtCore import Qt, QModelIndex, QAbstractTableModel, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QProgressBar, QLineEdit, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QWidget, QGridLayout,
)

import config
from .base_exporter import BaseExporter, get_video_info_and_keyframe
from workers import FFmpegWorker, MetadataWorker


class BulkExportDialog(BaseExporter):
    def __init__(self, parent, search_results: list):
        super().__init__(parent)

        self.search_results = search_results
        self._metadata = []
        self._export_count = 0
        self._total_exports = 0
        self._current_worker = None
        self._metadata_worker = None
        self._current_scene_idx = 0

        self.setWindowTitle('Bulk Export Scenes')
        self.resize(800, 600)
        self.setMinimumWidth(600)

        self._build_ui()
        self._start_metadata_analysis()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self._build_scene_table(layout)
        self._build_mode_section(layout)
        self._build_video_options(layout)
        self._build_audio_options(layout)
        self._build_progress_section(layout)
        self._build_button_section(layout, export_text='Export Selected')

        self._update_widget_states()

    def _build_scene_table(self, layout):
        group = QGroupBox('Scenes')
        group_layout = QVBoxLayout(group)

        sel_layout = QHBoxLayout()
        self._select_all_btn = QPushButton('Select All')
        self._select_all_btn.clicked.connect(self._select_all)
        sel_layout.addWidget(self._select_all_btn)

        self._deselect_all_btn = QPushButton('Deselect All')
        self._deselect_all_btn.clicked.connect(self._deselect_all)
        sel_layout.addWidget(self._deselect_all_btn)

        sel_layout.addStretch()
        group_layout.addLayout(sel_layout)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(['', 'Video', 'Scene Time', 'Resolution', 'Duration'])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        group_layout.addWidget(self._table)

        self._analysis_label = QLabel('Analyzing scenes...')
        group_layout.addWidget(self._analysis_label)

        layout.addWidget(group)

    def _build_progress_section(self, layout):
        self._scene_progress = QProgressBar()
        self._scene_progress.setMaximum(100)
        layout.addWidget(self._scene_progress)

        self._overall_progress = QProgressBar()
        self._overall_progress.setMaximum(100)
        layout.addWidget(self._overall_progress)

        self._status_label = QLabel('Ready')
        layout.addWidget(self._status_label)

        self._keyframe_info_label = QLabel()
        self._keyframe_info_label.setStyleSheet('font-size: 8pt;')
        layout.addWidget(self._keyframe_info_label)

    def _populate_table(self):
        self._table.setRowCount(len(self.search_results))
        for i, (video_path, score, ftype, rescore, scene_idx_raw, scene_time, scene_end, thumb_bytes, scene_source_db) in enumerate(self.search_results):
            checkbox = QTableWidgetItem()
            checkbox.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            checkbox.setCheckState(Qt.Checked)
            self._table.setItem(i, 0, checkbox)

            self._table.setItem(i, 1, QTableWidgetItem(os.path.basename(video_path)))
            self._table.setItem(i, 2, QTableWidgetItem(f'{scene_time}s - {scene_end}s'))

            meta = self._metadata[i] if i < len(self._metadata) else None
            if meta and not meta.get('error'):
                self._table.setItem(i, 3, QTableWidgetItem(f'{meta["width"]}x{meta["height"]}'))
                self._table.setItem(i, 4, QTableWidgetItem(f'{meta["duration_ms"] // 1000}s'))
            else:
                self._table.setItem(i, 3, QTableWidgetItem('Analyzing...'))
                self._table.setItem(i, 4, QTableWidgetItem(''))
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

    def _select_all(self):
        for row in range(self._table.rowCount()):
            self._table.item(row, 0).setCheckState(Qt.Checked)

    def _deselect_all(self):
        for row in range(self._table.rowCount()):
            self._table.item(row, 0).setCheckState(Qt.Unchecked)

    def _start_metadata_analysis(self):
        self._analysis_label.setText('Analyzing scenes...')
        self._export_btn.setEnabled(False)

        raw_scenes = []
        for (video_path, score, ftype, rescore, scene_idx_raw, scene_time, scene_end, thumb_bytes, scene_source_db) in self.search_results:
            raw_scenes.append((video_path, scene_time * 1000, scene_end * 1000))

        self._metadata_worker = MetadataWorker(raw_scenes)
        self._metadata_worker.progress.connect(self._on_metadata_progress, type=Qt.QueuedConnection)
        self._metadata_worker.metadata_finished.connect(self._on_metadata_finished, type=Qt.QueuedConnection)
        self._metadata_worker.cancelled_signal.connect(self.reject, type=Qt.QueuedConnection)
        self._metadata_worker.start()

    def _on_metadata_progress(self, current: int, total: int, filename: str):
        self._analysis_label.setText(f'Analyzing scene {current}/{total}: {filename}')

    def _on_metadata_finished(self, metadata_list: list):
        self._metadata = metadata_list
        self._populate_table()
        self._analysis_label.setText(f'Analysis complete. {len(self._metadata)} scenes ready.')
        self._export_btn.setEnabled(True)

    def _save_settings(self):
        self._save_common_settings()
        config.save_config(self.config)

    def _start_export(self):
        selected_indices = []
        for row in range(self._table.rowCount()):
            if self._table.item(row, 0).checkState() == Qt.Checked:
                selected_indices.append(row)

        if not selected_indices:
            QMessageBox.information(self, 'No Scenes', 'No scenes selected for export.')
            return

        self._save_settings()

        self._export_btn.setEnabled(False)
        self._select_all_btn.setEnabled(False)
        self._deselect_all_btn.setEnabled(False)
        self._mode_copy.setEnabled(False)
        self._mode_encode.setEnabled(False)
        self._cancel_btn.setText('Cancel')
        self._cancel_btn.setEnabled(True)
        self.cancelled = False

        self._total_exports = len(selected_indices)
        self._export_count = 0
        self._export_queue = selected_indices

        self._overall_progress.setMaximum(self._total_exports)
        self._overall_progress.setValue(0)

        self._export_next()

    def _export_next(self):
        if not self._export_queue or self.cancelled:
            self._on_bulk_finished()
            return

        self._current_scene_idx = self._export_queue.pop(0)
        video_path, score, ftype, rescore, scene_idx_raw, scene_time, scene_end, thumb_bytes, scene_source_db = \
            self.search_results[self._current_scene_idx]

        self._status_label.setText(f'Exporting scene {self._export_count + 1}/{self._total_exports}: '
                                   f'{os.path.basename(video_path)}')
        self._scene_progress.setValue(0)

        start_ms = scene_time * 1000
        end_ms = scene_end * 1000
        duration_ms = end_ms - start_ms

        default_name = f'{os.path.splitext(os.path.basename(video_path))[0]}_scene_{scene_time}s-{scene_end}s.mp4'
        output_dir = self._get_output_dir()
        output_path = os.path.join(output_dir, default_name)

        cmd = self._build_scene_command(video_path, start_ms, end_ms, duration_ms, output_path)
        self._current_worker = FFmpegWorker(cmd, duration_ms)
        self._current_worker.progress_updated.connect(self._on_scene_progress, type=Qt.QueuedConnection)
        self._current_worker.export_finished.connect(self._on_scene_finished, type=Qt.QueuedConnection)
        self._current_worker.error.connect(self._on_scene_error, type=Qt.QueuedConnection)
        self._current_worker.start()

    def _build_scene_command(self, video_path: str, start_ms: int, end_ms: int,
                              duration_ms: int, output_path: str) -> list:
        cmd = [self._get_ffmpeg_path()]
        if self._mode_copy.isChecked():
            meta = get_video_info_and_keyframe(video_path, start_ms)
            start_sec = meta['keyframe_ms'] / 1000.0
            cmd.extend(['-ss', str(start_sec), '-i', video_path, '-c', 'copy'])
        else:
            start_sec = start_ms / 1000.0
            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek
            cmd.extend(['-ss', str(fast_seek), '-i', video_path, '-ss', str(exact_seek)])
            meta = get_video_info_and_keyframe(video_path, start_ms)
            cmd.extend(self._get_core_ffmpeg_args(meta))
        duration_sec = duration_ms / 1000.0
        cmd.extend(['-t', str(duration_sec), output_path])
        return cmd

    def _get_output_dir(self) -> str:
        if self.search_results:
            first_path = self.search_results[0][0]
            base_dir = os.path.dirname(first_path)
            export_dir = os.path.join(base_dir, 'SceneScout_Exports')
            os.makedirs(export_dir, exist_ok=True)
            return export_dir
        return os.getcwd()

    def _on_scene_progress(self, progress: float, status: str):
        self._scene_progress.setValue(int(progress))
        self._status_label.setText(f'Exporting scene {self._export_count + 1}/{self._total_exports}: {status}')

    def _on_scene_finished(self):
        self._export_count += 1
        self._overall_progress.setValue(self._export_count)
        self._status_label.setText(f'Completed {self._export_count}/{self._total_exports}')
        QTimer.singleShot(0, self._export_next)

    def _on_scene_error(self, error_msg: str):
        self._status_label.setText(f'Error on scene {self._export_count + 1}')
        QMessageBox.critical(self, 'Export Error', error_msg)
        self._export_count += 1
        self._overall_progress.setValue(self._export_count)
        QTimer.singleShot(0, self._export_next)

    def _on_bulk_finished(self):
        if self.cancelled:
            self._status_label.setText('Cancelled')
        else:
            self._status_label.setText(f'Exported {self._export_count}/{self._total_exports} scenes')
            if self._open_folder_check.isChecked():
                output_dir = self._get_output_dir()
                try:
                    if sys.platform == 'win32':
                        subprocess.run(['explorer', output_dir])
                    elif sys.platform == 'darwin':
                        subprocess.run(['open', output_dir])
                    else:
                        subprocess.run(['xdg-open', output_dir])
                except Exception as e:
                    print(f'Failed to open output directory: {e}')

            QMessageBox.information(
                self, 'Bulk Export Complete',
                f'Successfully exported {self._export_count} of {self._total_exports} scenes.'
            )

        self._export_btn.setEnabled(True)
        self._select_all_btn.setEnabled(True)
        self._deselect_all_btn.setEnabled(True)
        self._mode_copy.setEnabled(True)
        self._mode_encode.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._current_worker = None

    def _on_cancel(self):
        if self._current_worker and self._current_worker.isRunning():
            self._status_label.setText('Cancelling...')
            self._cancel_btn.setEnabled(False)
            self.cancelled = True
            self._current_worker.cancel()
        if self._metadata_worker and self._metadata_worker.isRunning():
            self._metadata_worker.cancel()
        self.reject()
