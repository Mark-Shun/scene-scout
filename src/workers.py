import os
import re
import subprocess
import sys
from typing import Callable, Optional, List, Tuple, Any

from PySide6.QtCore import QThread, Signal, QObject, Slot


class SignalBridge(QObject):
    """Bridges Qt signals from QThread workers to regular Python callables
    on the main thread. Create this in the main thread, connect worker
    signals to its relay signals via QueuedConnection, then set callbacks
    that will be invoked on the main thread."""

    relay_status = Signal(str)
    relay_progress = Signal(object)
    relay_finished = Signal(object)
    relay_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status_cb: Optional[Callable] = None
        self._progress_cb: Optional[Callable] = None
        self._finished_cb: Optional[Callable] = None
        self._error_cb: Optional[Callable] = None

        self.relay_status.connect(self._on_status)
        self.relay_progress.connect(self._on_progress)
        self.relay_finished.connect(self._on_finished)
        self.relay_error.connect(self._on_error)

    def set_callbacks(self, status=None, progress=None, finished=None, error=None):
        self._status_cb = status
        self._progress_cb = progress
        self._finished_cb = finished
        self._error_cb = error

    @Slot(str)
    def _on_status(self, msg: str):
        if self._status_cb:
            self._status_cb(msg)

    @Slot(object)
    def _on_progress(self, data):
        if self._progress_cb:
            self._progress_cb(data)

    @Slot(object)
    def _on_finished(self, result: Any):
        if self._finished_cb:
            self._finished_cb(result)

    @Slot(str)
    def _on_error(self, msg: str):
        if self._error_cb:
            self._error_cb(msg)


class WorkerSignals(QObject):
    """Declares typed communication channels for QThread workers."""
    status_updated = Signal(str)
    progress_updated = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class ModelLoadWorker(QThread):
    def __init__(self, device_choice: str, use_trt: bool):
        super().__init__()
        self.signals = WorkerSignals()
        self.device_choice = device_choice
        self.use_trt = use_trt

    def run(self):
        try:
            from model_loader import load_siglip_model
            self.signals.status_updated.emit(f'Loading model...')
            model, processor, device, dtype, last_active = load_siglip_model(
                self.device_choice,
                status_callback=self.signals.status_updated.emit,
                use_trt=self.use_trt,
            )
            self.signals.finished.emit((model, processor, device, dtype, last_active))
        except Exception as e:
            self.signals.error.emit(str(e))


class IndexWorker(QThread):
    def __init__(self, device, processor, model, db_path: str, batch_size: int,
                 generate_thumbnails: bool, max_num_patches: int, fast_scene_detect: bool):
        super().__init__()
        self.signals = WorkerSignals()
        self.device = device
        self.processor = processor
        self.model = model
        self.db_path = db_path
        self.batch_size = batch_size
        self.generate_thumbnails = generate_thumbnails
        self.max_num_patches = max_num_patches
        self.fast_scene_detect = fast_scene_detect

    def run(self):
        try:
            from processing import index_files
            self.signals.status_updated.emit('Indexing files...')
            result = index_files(
                self.device, self.processor, self.model, self.db_path,
                batch_size=self.batch_size,
                generate_thumbnails=self.generate_thumbnails,
                progress_callback=self.signals.progress_updated.emit,
                max_num_patches=self.max_num_patches,
                fast_scene_detect=self.fast_scene_detect,
                toggle_preview_callback=None,
                cancel_check=lambda: self.isInterruptionRequested(),
            )
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))


class SearchWorker(QThread):
    def __init__(self, query_text: str, query_image_path: Optional[str],
                 device, processor, model, active_databases: List[str],
                 top_k: int, max_patches: int):
        super().__init__()
        self.signals = WorkerSignals()
        self.query_text = query_text
        self.query_image_path = query_image_path
        self.device = device
        self.processor = processor
        self.model = model
        self.active_databases = active_databases
        self.top_k = top_k
        self.max_patches = max_patches

    def run(self):
        try:
            from processing import get_query_embedding
            from database import search_scenes
            self.signals.status_updated.emit('Generating query embedding...')
            query_embedding = get_query_embedding(
                self.query_text, self.query_image_path,
                self.device, self.processor, self.model,
                max_num_patches=self.max_patches,
            )
            if query_embedding is None:
                raise ValueError('Could not generate query embedding.')
            self.signals.status_updated.emit('Searching databases...')
            results = search_scenes(
                query_embedding, self.active_databases, top_k=self.top_k
            )
            self.signals.finished.emit(results)
        except Exception as e:
            self.signals.error.emit(str(e))


class RescoreWorker(QThread):
    def __init__(self, search_results: list, query_text: str, primary_db: str,
                 device, processor, model):
        super().__init__()
        self.signals = WorkerSignals()
        self.search_results = search_results
        self.query_text = query_text
        self.primary_db = primary_db
        self.device = device
        self.processor = processor
        self.model = model

    def run(self):
        try:
            from processing import get_query_embedding
            import sqlite3
            import numpy as np

            rescore_embedding = get_query_embedding(
                self.query_text, None, self.device, self.processor, self.model,
            )
            if rescore_embedding is None:
                raise ValueError('Could not generate rescore embedding.')

            updated = []
            with sqlite3.connect(self.primary_db) as conn:
                cursor = conn.cursor()
                for path, score, ftype, _, scene_idx, scene_time, scene_end, thumb_bytes, source_db in self.search_results:
                    new_score = None
                    if ftype == 'image':
                        cursor.execute(
                            'SELECT embedding FROM image_embeddings WHERE filepath=?', (path,)
                        )
                        row = cursor.fetchone()
                        if row:
                            emb = np.frombuffer(row[0], dtype=np.float32)
                            new_score = float(np.dot(emb, rescore_embedding.T).squeeze())
                    elif ftype == 'video':
                        if isinstance(scene_idx, tuple):
                            start_idx, end_idx = scene_idx
                            cursor.execute('''
                                SELECT se.embedding FROM scene_embeddings se
                                JOIN processed_videos pv ON se.video_id = pv.id
                                WHERE pv.filepath=? AND se.scene_index >= ? AND se.scene_index <= ?
                            ''', (path, start_idx, end_idx))
                            rows = cursor.fetchall()
                            if rows:
                                max_sim = -1.0
                                for row in rows:
                                    emb = np.frombuffer(row[0], dtype=np.float32)
                                    sim = float(np.dot(emb, rescore_embedding.T).squeeze())
                                    if sim > max_sim:
                                        max_sim = sim
                                new_score = max_sim
                        else:
                            cursor.execute('''
                                SELECT se.embedding FROM scene_embeddings se
                                JOIN processed_videos pv ON se.video_id = pv.id
                                WHERE pv.filepath=? AND se.scene_index=?
                            ''', (path, scene_idx))
                            row = cursor.fetchone()
                            if row:
                                emb = np.frombuffer(row[0], dtype=np.float32)
                                new_score = float(np.dot(emb, rescore_embedding.T).squeeze())
                    updated.append((path, score, ftype, new_score, scene_idx,
                                    scene_time, scene_end, thumb_bytes, source_db))
            self.signals.finished.emit(updated)
        except Exception as e:
            self.signals.error.emit(str(e))


class FFmpegWorker(QThread):
    """Runs a single FFmpeg command in a background thread, parsing stderr
    for time progress and supporting cancellation."""

    progress_updated = Signal(float, str)
    export_finished = Signal()
    error = Signal(str)

    def __init__(self, cmd: list, duration_ms: int):
        super().__init__()
        self.cmd = cmd
        self.duration_ms = duration_ms
        self._process: Optional[subprocess.Popen] = None

    def run(self):
        creation_flags = 0
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW

        time_regex = re.compile(r'time=(\d+:\d+:\d+\.\d+)')
        stderr_lines: list[str] = []

        try:
            self._process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
                bufsize=1,
                universal_newlines=True,
            )

            process = self._process
            for line in process.stderr:
                stderr_lines.append(line)
                if self.isInterruptionRequested():
                    process.terminate()
                    process.wait()
                    return
                match = time_regex.search(line)
                if match:
                    current_ms = self._parse_time_to_ms(match.group(1))
                    pct = min(100.0, (current_ms / self.duration_ms) * 100.0) if self.duration_ms else 0.0
                    self.progress_updated.emit(pct, f'Exporting... ({match.group(1)})')

            process.wait()
            if process.returncode == 0 and not self.isInterruptionRequested():
                self.export_finished.emit()
            elif not self.isInterruptionRequested():
                stderr_output = ''.join(stderr_lines[-80:])
                self.error.emit(f'FFmpeg exited with code {process.returncode}\n\n{stderr_output}')
        except Exception as e:
            self.error.emit(str(e))

    def cancel(self):
        self.requestInterruption()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    @staticmethod
    def _parse_time_to_ms(time_str: str) -> int:
        parts = time_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        sec_parts = parts[2].split('.')
        seconds = int(sec_parts[0])
        millis = int(sec_parts[1]) if len(sec_parts) > 1 else 0
        return ((hours * 3600 + minutes * 60 + seconds) * 1000) + millis


class MetadataWorker(QThread):
    """Analyzes video metadata for each scene in the list (bulk export preview)."""

    metadata_finished = Signal(object)
    progress = Signal(int, int, str)
    cancelled_signal = Signal()

    def __init__(self, scenes: list):
        super().__init__()
        self.scenes = scenes

    def run(self):
        from exporters.base_exporter import get_video_info_and_keyframe
        temp_metadata = []
        total = len(self.scenes)
        for i, (video_path, start_ms, _end_ms) in enumerate(self.scenes):
            if self.isInterruptionRequested():
                self.cancelled_signal.emit()
                return
            self.progress.emit(i + 1, total, os.path.basename(video_path))
            meta = get_video_info_and_keyframe(video_path, start_ms)
            temp_metadata.append(meta)
        self.metadata_finished.emit(temp_metadata)


class CombineDBWorker(QThread):
    def __init__(self, active_databases: list, out_path: str):
        super().__init__()
        self.signals = WorkerSignals()
        self.active_databases = active_databases
        self.out_path = out_path

    def run(self):
        try:
            from database import combine_databases
            combine_databases(
                self.active_databases,
                self.out_path,
                self.signals.status_updated.emit,
            )
            self.signals.finished.emit(self.out_path)
        except Exception as e:
            self.signals.error.emit(str(e))


class VerifyPathsWorker(QThread):
    def __init__(self, active_databases: list):
        super().__init__()
        self.signals = WorkerSignals()
        self.active_databases = active_databases

    def run(self):
        try:
            from database import get_all_processed_videos
            missing = []
            for db_path in self.active_databases:
                videos = get_all_processed_videos(db_path)
                for video_id, filepath in videos:
                    if not os.path.exists(filepath):
                        missing.append((db_path, video_id, filepath))
            self.signals.finished.emit(missing)
        except Exception as e:
            self.signals.error.emit(str(e))


class CleanupWorker(QThread):
    def __init__(self, primary_db: str):
        super().__init__()
        self.signals = WorkerSignals()
        self.primary_db = primary_db

    def run(self):
        try:
            from database import cleanup_orphaned_entries
            count = cleanup_orphaned_entries(
                self.primary_db, self.signals.status_updated.emit
            )
            self.signals.finished.emit(count)
        except Exception as e:
            self.signals.error.emit(str(e))
