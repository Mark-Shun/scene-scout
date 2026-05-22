import os
import sys
import subprocess

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QLabel, QProgressBar, QMessageBox,
)

import config
from .base_exporter import BaseExporter, get_video_info_and_keyframe
from workers import FFmpegWorker


class BulkExportDialog(BaseExporter):
    def __init__(self, parent, search_results: list):
        super().__init__(parent)

        self.search_results = search_results
        self._metadata = [
            get_video_info_and_keyframe(vp, st)
            for vp, _, _, _, _, st, _, _, _ in search_results
        ]
        self._export_count = 0
        self._total_exports = 0
        self._current_worker = None
        self._current_scene_idx = 0
        self.planned_outputs = []

        self.setWindowTitle('Bulk Export Scenes')
        self.setMinimumWidth(500)

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self._build_container_section(layout)
        self._build_mode_section(layout)
        self._build_naming_section(layout, is_bulk=True)
        
        self._build_video_options(layout)
        self._build_audio_options(layout)
        self._build_progress_section(layout)
        self._build_button_section(layout, export_text='Export Selected')
        
        self._output_dir_edit.setText(self._get_initial_output_dir())
        self._update_widget_states()
        self._update_preview_display()

    def _get_preview_params(self):
        v_path = self.search_results[0][0]
        s_ms = self.search_results[0][5]
        e_ms = self.search_results[0][6]
        return self._metadata[0], v_path, s_ms, e_ms

    def _get_initial_output_dir(self) -> str:
        if self.search_results:
            first_path = self.search_results[0][0]
            base_dir = os.path.dirname(first_path)
            return os.path.join(base_dir, 'SceneScout_Exports')
        return os.getcwd()

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

    def _save_settings(self):
        self._save_common_settings()
        config.save_config(self.config)

    def _generate_default_output_path(self, video_path: str, start_ms: int, end_ms: int, scene_idx: int = 0) -> str:
        metadata = self._metadata[scene_idx] if scene_idx < len(self._metadata) else {}
        template = self._template_edit.text()
        filename = self._resolve_naming_template(template, metadata, video_path, start_ms, end_ms, scene_idx)
        ext = self.CONTAINERS.get(self._container_combo.currentText(), '.mp4')
        return os.path.join(self._output_dir_edit.text(), f"{filename}{ext}")

    def _make_unique_output_path(self, output_path: str, reserved_paths: set) -> str:
        if output_path not in reserved_paths and not os.path.exists(output_path):
            return output_path
        folder = os.path.dirname(output_path)
        stem, ext = os.path.splitext(os.path.basename(output_path))
        counter = 2
        while True:
            candidate = os.path.join(folder, f'{stem}_{counter}{ext}')
            if candidate not in reserved_paths and not os.path.exists(candidate):
                return candidate
            counter += 1

    def _start_export(self):
        selected_indices = list(range(len(self.search_results)))

        if not selected_indices:
            QMessageBox.information(self, 'No Scenes', 'No scenes selected for export.')
            return

        output_dir = self._output_dir_edit.text()
        if not output_dir:
            QMessageBox.critical(self, 'Error', 'Please specify an output folder.')
            return

        os.makedirs(output_dir, exist_ok=True)

        # Build paths securely, check for overwrites upfront
        planned_outputs = []
        reserved_paths = set()
        
        for row in selected_indices:
            video_path, score, ftype, rescore, scene_idx_raw, scene_time, scene_end, thumb_bytes, scene_source_db = self.search_results[row]
            output_path = self._make_unique_output_path(
                self._generate_default_output_path(video_path, scene_time, scene_end, row),
                reserved_paths
            )
            planned_outputs.append(output_path)
            reserved_paths.add(output_path)
            
        existing_outputs = [p for p in planned_outputs if os.path.exists(p)]
        if existing_outputs:
            shown = '\n'.join(os.path.basename(p) for p in existing_outputs[:8])
            if len(existing_outputs) > 8:
                shown += f'\n...and {len(existing_outputs) - 8} more'

            reply = QMessageBox.question(
                self, 'Overwrite?',
                f'{len(existing_outputs)} output file(s) already exist:\n\n{shown}\n\nOverwrite them?',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
                
        self.planned_outputs = planned_outputs
        self._save_settings()

        self._export_btn.setEnabled(False)
        self._mode_copy.setEnabled(False)
        self._mode_encode.setEnabled(False)
        self._cancel_btn.setText('Cancel')
        self._cancel_btn.setEnabled(True)
        self.cancelled = False

        self._total_exports = len(selected_indices)
        self._export_count = 0
        self._export_queue = selected_indices.copy()

        self._overall_progress.setMaximum(self._total_exports)
        self._overall_progress.setValue(0)

        self._export_next()

    def _export_next(self):
        if not self._export_queue or self.cancelled:
            self._on_bulk_finished()
            return

        # Pop from queue but figure out index in the static planned_outputs
        queue_index = self._total_exports - len(self._export_queue) 
        self._current_scene_idx = self._export_queue.pop(0)
        output_path = self.planned_outputs[queue_index]
        
        video_path, score, ftype, rescore, scene_idx_raw, scene_time, scene_end, thumb_bytes, scene_source_db = \
            self.search_results[self._current_scene_idx]

        self._status_label.setText(f'Exporting scene {self._export_count + 1}/{self._total_exports}: '
                                   f'{os.path.basename(video_path)}')
        self._scene_progress.setValue(0)

        start_ms = scene_time
        end_ms = scene_end
        duration_ms = end_ms - start_ms

        cmd = self._build_scene_command(video_path, start_ms, end_ms, duration_ms, output_path, self._current_scene_idx)
        
        self._current_worker = FFmpegWorker(cmd, duration_ms)
        self._current_worker.progress_updated.connect(self._on_scene_progress, type=Qt.QueuedConnection)
        self._current_worker.export_finished.connect(self._on_scene_finished, type=Qt.QueuedConnection)
        self._current_worker.error.connect(self._on_scene_error, type=Qt.QueuedConnection)
        self._current_worker.start()

    def _build_scene_command(self, video_path: str, start_ms: int, end_ms: int,
                              duration_ms: int, output_path: str, scene_idx: int) -> list:
        cmd = [self._get_ffmpeg_path()]
        meta = self._metadata[scene_idx] if scene_idx < len(self._metadata) else {}
        
        if self._mode_copy.isChecked():
            start_sec = meta.get('keyframe_ms', start_ms) / 1000.0
            cmd.extend(['-ss', str(start_sec), '-i', video_path, '-c', 'copy'])
        else:
            start_sec = start_ms / 1000.0
            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek
            cmd.extend(['-ss', str(fast_seek), '-i', video_path, '-ss', str(exact_seek)])
            cmd.extend(self._get_core_ffmpeg_args(meta))
            
        duration_sec = duration_ms / 1000.0
        cmd.extend(['-t', str(duration_sec), output_path])
        return cmd

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
                output_dir = self._output_dir_edit.text()
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
        else:
            self.reject()
