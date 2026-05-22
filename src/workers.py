import logging
import os
import re
import subprocess
import sys
import zipfile
import shutil
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
            logging.exception("Fatal error in ModelLoadWorker")
            self.signals.error.emit(str(e))


class IndexWorker(QThread):
    def __init__(self, device, processor, model, db_path: str, batch_size: int,
                 generate_thumbnails: bool, max_num_patches: int, fast_scene_detect: bool,
                 frames_per_scene: int = 3, force_reprocess: bool = False):
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
        self.frames_per_scene = frames_per_scene
        self.force_reprocess = force_reprocess

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
                frames_per_scene=self.frames_per_scene,
                force_reprocess=self.force_reprocess,
                toggle_preview_callback=None,
                cancel_check=lambda: self.isInterruptionRequested(),
            )
            self.signals.finished.emit(result)
        except Exception as e:
            logging.exception("Fatal error in IndexWorker")
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
            logging.exception("Fatal error in SearchWorker")
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
            logging.exception("Fatal error in RescoreWorker")
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
            logging.exception("Fatal error in FFmpegWorker")
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
            logging.exception("Fatal error in CombineDBWorker")
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
            logging.exception("Fatal error in VerifyPathsWorker")
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
            logging.exception("Fatal error in CleanupWorker")
            self.signals.error.emit(str(e))


class ArchiveExportWorker(QThread):
    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, db_paths: list, output_scdb_path: str):
        super().__init__()
        self.db_paths = db_paths  # Now accepts a LIST of database paths
        self.output_path = output_scdb_path
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            import sqlite3

            # 1. Deduplicate Videos across ALL selected databases
            self.progress.emit(0, "Analyzing databases for shared assets...")
            unique_videos = set()

            for db_path in self.db_paths:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.execute("SELECT filepath FROM processed_videos WHERE status='completed'")
                    # Only add videos that actually exist on disk
                    unique_videos.update(row[0] for row in cursor.fetchall() if os.path.exists(row[0]))

            total_items = len(self.db_paths) + len(unique_videos)
            processed = 0

            # 2. Pack the archive using ZIP_STORED to prevent CPU bottleneck and double-compression
            with zipfile.ZipFile(self.output_path, 'w', zipfile.ZIP_STORED) as archive:
                # Add all selected databases to the root of the archive
                for db_path in self.db_paths:
                    if self._is_cancelled: return
                    db_name = os.path.basename(db_path)
                    self.progress.emit(int((processed / total_items) * 100), f"Packing database: {db_name}")
                    archive.write(db_path, db_name)
                    processed += 1

                # Add all unique video files to a 'videos' folder inside the archive
                for path in unique_videos:
                    if self._is_cancelled: return
                    filename = os.path.basename(path)
                    self.progress.emit(int((processed / total_items) * 100), f"Archiving video: {filename}")
                    archive.write(path, f"videos/{filename}")
                    processed += 1

            self.progress.emit(100, "Done!")
            self.finished.emit()
        except Exception as e:
            logging.exception("Fatal error in ArchiveExportWorker")
            self.error.emit(str(e))


class ArchiveImportWorker(QThread):
    progress = Signal(int, str)
    finished = Signal(list)  # Returns a list of the newly unpacked database paths
    error = Signal(str)

    def __init__(self, scdb_path: str, target_extraction_dir: str):
        super().__init__()
        self.scdb_path = scdb_path
        self.target_dir = target_extraction_dir

    def run(self):
        try:
            from database import remap_all_video_paths

            if not os.path.exists(self.scdb_path):
                raise FileNotFoundError("Source archive file could not be found.")

            # Space verification
            archive_size = os.path.getsize(self.scdb_path)
            check_path = self.target_dir
            while check_path and not os.path.exists(check_path):
                check_path = os.path.dirname(check_path)
            if not check_path:
                check_path = os.getcwd()

            _, _, free_bytes = shutil.disk_usage(check_path)
            required_space_with_buffer = int(archive_size * 1.05)

            if free_bytes < required_space_with_buffer:
                req_mb = required_space_with_buffer / (1024 * 1024)
                free_mb = free_bytes / (1024 * 1024)
                raise IOError(f"Insufficient disk space.\nRequired: ~{req_mb:.1f} MB\nAvailable: {free_mb:.1f} MB")

            os.makedirs(self.target_dir, exist_ok=True)

            # Extract everything
            with zipfile.ZipFile(self.scdb_path, 'r') as archive:
                namelist = archive.namelist()
                total_files = len(namelist)

                for idx, member in enumerate(namelist):
                    self.progress.emit(int((idx / total_files) * 100), f"Unpacking: {os.path.basename(member)}")
                    archive.extract(member, self.target_dir)

            # Find all the databases we just extracted
            extracted_dbs = [f for f in namelist if f.endswith('.db') and '/' not in f]
            videos_folder = os.path.join(self.target_dir, "videos")

            final_db_paths = []

            # Remap paths for EVERY database in the archive
            for db_name in extracted_dbs:
                db_path = os.path.join(self.target_dir, db_name)
                self.progress.emit(95, f"Recalibrating paths for {db_name}...")
                remap_all_video_paths(db_path, videos_folder)
                final_db_paths.append(db_path)

            self.progress.emit(100, "Unpack successful!")
            self.finished.emit(final_db_paths)

        except Exception as e:
            logging.exception("Fatal error in ArchiveImportWorker")
            self.error.emit(str(e))
