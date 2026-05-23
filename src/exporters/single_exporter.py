import os
import sys
import subprocess

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QProgressBar, QLineEdit, QFileDialog, QMessageBox, QWidget,
    QScrollArea, QFrame,
)

import config
from .base_exporter import BaseExporter, get_video_info_and_keyframe
from workers import FFmpegWorker


class SingleExportDialog(BaseExporter):
    def __init__(self, parent, video_path: str, start_ms: int, end_ms: int):
        super().__init__(parent)

        self.video_path = video_path
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.duration_ms = end_ms - start_ms
        self.metadata = get_video_info_and_keyframe(self.video_path, self.start_ms)
        self._ffmpeg_worker = None

        self.setWindowTitle('Export Scene')
        self.setMinimumWidth(500)
        self.resize(500, 700)

        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)

        self._build_container_section(scroll_layout)
        self._build_mode_section(scroll_layout)
        self._build_naming_section(scroll_layout, is_bulk=False)
        self._build_video_options(scroll_layout)
        self._build_audio_options(scroll_layout)
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        self._build_progress_section(main_layout)
        self._build_button_section(main_layout, export_text='Export')

        self._update_widget_states()
        self._update_preview_display()

    def _get_preview_params(self):
        return self.metadata, self.video_path, self.start_ms, self.end_ms

    def _build_progress_section(self, layout):
        self._progress_bar = QProgressBar()
        self._progress_bar.setMaximum(100)
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel('Ready')
        layout.addWidget(self._status_label)

        self._keyframe_info_label = QLabel(self._get_keyframe_info())
        self._keyframe_info_label.setStyleSheet('font-size: 8pt;')
        layout.addWidget(self._keyframe_info_label)

    def _get_keyframe_info(self) -> str:
        if self._mode_copy.isChecked():
            return (
                f"Note: Stream Copy snaps to keyframe at "
                f"{self._format_ms(self.metadata['keyframe_ms'])}, timing may not be exact"
            )
        return f'Exact frame accuracy: {self._format_ms(self.start_ms)}'

    def _save_settings(self):
        self._save_common_settings()
        config.save_config(self.config)

    def _start_export(self):
        output_path = self._output_dir_edit.text()
        if not output_path:
            QMessageBox.critical(self, 'Error', 'Please specify an output path.')
            return

        if os.path.exists(output_path):
            reply = QMessageBox.question(
                self, 'Overwrite?',
                f'{os.path.basename(output_path)} already exists. Overwrite?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._save_settings()

        self._export_btn.setEnabled(False)
        self._cancel_btn.setText('Cancel')
        self._cancel_btn.setEnabled(True)
        self.cancelled = False

        self._progress_bar.setValue(0)
        self._status_label.setText('Starting export...')

        cmd = self._build_ffmpeg_command()
        self._ffmpeg_worker = FFmpegWorker(cmd, self.duration_ms)
        self._ffmpeg_worker.progress_updated.connect(self._update_progress, type=Qt.QueuedConnection)
        self._ffmpeg_worker.export_finished.connect(self._on_export_complete, type=Qt.QueuedConnection)
        self._ffmpeg_worker.error.connect(self._on_export_error, type=Qt.QueuedConnection)
        self._ffmpeg_worker.start()

    def _build_ffmpeg_command(self) -> list:
        cmd = [self._get_ffmpeg_path()]
        if self._mode_copy.isChecked():
            start_sec = self.metadata['keyframe_ms'] / 1000.0
            cmd.extend(['-ss', str(start_sec), '-i', self.video_path, '-c', 'copy'])
        else:
            start_sec = self.start_ms / 1000.0
            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek
            cmd.extend(['-ss', str(fast_seek), '-i', self.video_path, '-ss', str(exact_seek)])
            cmd.extend(self._get_core_ffmpeg_args(self.metadata))
            
        duration_sec = self.duration_ms / 1000.0
        cmd.extend(['-t', str(duration_sec), self._output_dir_edit.text()])
        return cmd

    def _update_progress(self, progress: float, status: str):
        self._progress_bar.setValue(int(progress))
        self._status_label.setText(status)

    def _on_export_complete(self):
        self._progress_bar.setValue(100)
        self._status_label.setText('Export complete!')

        output_path = self._output_dir_edit.text()

        if self._open_folder_check.isChecked():
            output_abs = os.path.abspath(output_path)
            folder = os.path.dirname(output_abs)
            try:
                if sys.platform == 'win32':
                    subprocess.run(['explorer', '/select,', output_abs])
                elif sys.platform == 'darwin':
                    subprocess.run(['open', '-R', output_abs])
                else:
                    subprocess.run(['xdg-open', folder])
            except Exception as e:
                print(f'Failed to open output directory: {e}')

        self._ffmpeg_worker = None
        self.accept()

    def _on_export_error(self, error_msg: str):
        self._status_label.setText('Export failed!')
        QMessageBox.critical(self, 'Export Error', error_msg)
        self._export_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._ffmpeg_worker = None

    def _on_cancel(self):
        if self._ffmpeg_worker and self._ffmpeg_worker.isRunning():
            self._status_label.setText('Cancelling...')
            self._cancel_btn.setEnabled(False)
            self._ffmpeg_worker.cancel()
        else:
            self.reject()
