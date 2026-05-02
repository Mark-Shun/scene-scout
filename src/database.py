import os
import sqlite3
from typing import Callable, List, Optional, Tuple
import numpy as np

import config

DB_SCHEMA = f"""
-- store image embeddings (searchable items) as before
CREATE TABLE IF NOT EXISTS image_embeddings (
    filepath TEXT PRIMARY KEY,
    modified_at REAL NOT NULL,
    embedding BLOB NOT NULL,
    model_version TEXT DEFAULT '{config.DEFAULT_MODEL}',
    file_type TEXT DEFAULT 'image'
);

-- track which video files have been fully processed; scenes go into scene_embeddings
CREATE TABLE IF NOT EXISTS processed_videos (
    filepath TEXT PRIMARY KEY,
    modified_at REAL NOT NULL,
    model_version TEXT DEFAULT '{config.DEFAULT_MODEL}'
);

CREATE TABLE IF NOT EXISTS scene_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,
    scene_index INTEGER NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    FOREIGN KEY (filepath) REFERENCES processed_videos(filepath) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scene_filepath ON scene_embeddings(filepath);
"""

def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DB_SCHEMA)

def db_is_empty(db_path: str) -> bool:
    """Return True if there are no entries in `embeddings`,`processed_videos` or `scene_embeddings`."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM image_embeddings LIMIT 1")
            if cursor.fetchone():
                return False
            cursor.execute("SELECT 1 FROM processed_videos LIMIT 1")
            if cursor.fetchone():
                return False
            cursor.execute("SELECT 1 FROM scene_embeddings LIMIT 1")
            if cursor.fetchone():
                return False
            return True
    except Exception:
        # If the DB can't be opened or tables missing, treat as empty
        return True

def cleanup_orphaned_entries(db_path: str, progress_callback: Optional[Callable]=None) -> int:
    """Remove database rows for files that no longer exist.

    This handles both the image `embeddings` table and the
    `processed_videos` table. Scene entries are removed via cascade
    when the corresponding video record is deleted (foreign keys must
    be enabled on the connection).
    """
    total_removed = 0
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # ensure cascade behaviour works
        cursor.execute('PRAGMA foreign_keys = ON')

        # clean embeddings (images)
        cursor.execute('SELECT filepath FROM image_embeddings')
        all_paths = cursor.fetchall()
        if progress_callback:
            progress_callback(f'Checking {len(all_paths)} image entries in the database...')
        orphaned = [p for p, in all_paths if not os.path.exists(p)]
        if orphaned:
            if progress_callback:
                progress_callback(f'Removing {len(orphaned)} orphaned image embeddings...')
            cursor.executemany('DELETE FROM image_embeddings WHERE filepath=?', [(p,) for p in orphaned])
            conn.commit()
            total_removed += len(orphaned)
            if progress_callback:
                progress_callback(f'Cleanup complete: removed {len(orphaned)} orphaned image embeddings.')
        elif progress_callback:
            progress_callback('No orphaned image embeddings found.')

        # clean processed_videos (videos)
        cursor.execute('SELECT filepath FROM processed_videos')
        video_paths = cursor.fetchall()
        if progress_callback:
            progress_callback(f'Checking {len(video_paths)} processed video entries in the database...')
        orphaned_videos = [p for p, in video_paths if not os.path.exists(p)]
        if orphaned_videos:
            if progress_callback:
                progress_callback(f'Removing {len(orphaned_videos)} orphaned processed videos...')
            cursor.executemany('DELETE FROM processed_videos WHERE filepath=?', [(p,) for p in orphaned_videos])
            conn.commit()
            total_removed += len(orphaned_videos)
            if progress_callback:
                progress_callback(f'Cleanup complete: removed {len(orphaned_videos)} orphaned processed videos.')
        elif progress_callback:
            progress_callback('No orphaned processed videos found.')

    return total_removed

def search_db(query_embedding: np.ndarray, db_path: str, top_k: int=10, similarity_threshold: float=-1.0, batch_size: int=1000) -> List[Tuple[str, float, str]]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT filepath, embedding, file_type FROM image_embeddings')
        results = []
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            filepaths = [row[0] for row in batch]
            file_types = [row[2] for row in batch]
            db_embeddings = np.array([np.frombuffer(row[1], dtype=np.float32) for row in batch])
            if db_embeddings.ndim == 1:
                db_embeddings = db_embeddings.reshape(1, -1)
            similarities = np.dot(db_embeddings, query_embedding.T).squeeze()
            for i, sim in enumerate(similarities):
                if sim >= similarity_threshold:
                    results.append((filepaths[i], float(sim), file_types[i]))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]

def search_scenes(query_embedding: np.ndarray, db_path: str, top_k: int = 50, similarity_threshold: float = -1.0, batch_size: int = 1000) -> List[Tuple[str, int, int, int, float]]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT filepath, scene_index, start_time_ms, end_time_ms, embedding FROM scene_embeddings')
        results = []
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            filepaths = [row[0] for row in batch]
            scene_indices = [row[1] for row in batch]
            start_times = [row[2] for row in batch]
            end_times = [row[3] for row in batch]
            db_embeddings = np.array([np.frombuffer(row[4], dtype=np.float32) for row in batch])
            if db_embeddings.ndim == 1:
                db_embeddings = db_embeddings.reshape(1, -1)
            similarities = np.dot(db_embeddings, query_embedding.T).squeeze()
            for i, sim in enumerate(similarities):
                if sim >= similarity_threshold:
                    results.append((filepaths[i], scene_indices[i], start_times[i], end_times[i], thumbnails[i], float(sim)))
    results.sort(key=lambda x: x[5], reverse=True)
    return results[:top_k]
