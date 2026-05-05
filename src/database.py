import os
import sqlite3
from typing import Callable, List, Optional, Tuple
import numpy as np
from pathlib import Path
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
    thumbnail BLOB,
    FOREIGN KEY (filepath) REFERENCES processed_videos(filepath) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scene_filepath ON scene_embeddings(filepath);

-- index queue for tracking files/directories to process
CREATE TABLE IF NOT EXISTS index_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    is_directory BOOLEAN NOT NULL DEFAULT 0,
    recursive BOOLEAN NOT NULL DEFAULT 1,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(DB_SCHEMA)

    # Migration/healing check
    migrate_database(db_path)

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

def _search_db_single(query_embedding: np.ndarray, db_path: str, source_name: str, top_k: int=10, similarity_threshold: float=-1.0, batch_size: int=1000) -> List[Tuple[str, float, str, str]]:
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
                    results.append((filepaths[i], float(sim), file_types[i], source_name))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]

def search_db(query_embedding: np.ndarray, db_paths: List[str], top_k: int=10, similarity_threshold: float=-1.0, batch_size: int=1000) -> List[Tuple[str, float, str, str]]:
    all_results = []
    for db_path in db_paths:
        if not os.path.exists(db_path):
            continue
        source_name = os.path.basename(db_path)
        db_results = _search_db_single(query_embedding, db_path, source_name, top_k, similarity_threshold, batch_size)
        all_results.extend(db_results)
    
    all_results.sort(key=lambda x: x[1], reverse=True)
    
    seen = set()
    deduped = []
    for result in all_results:
        key = result[0]
        if key not in seen:
            seen.add(key)
            deduped.append(result)
    
    return deduped[:top_k]

def _search_scenes_single(query_embedding: np.ndarray, db_path: str, source_name: str, top_k: int = 50, similarity_threshold: float = -1.0, batch_size: int = 1000) -> List[Tuple[str, int, int, int, bytes, float, str]]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT filepath, scene_index, start_time_ms, end_time_ms, embedding, thumbnail FROM scene_embeddings')
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
            thumbnails = [row[5] for row in batch]

            if db_embeddings.ndim == 1:
                db_embeddings = db_embeddings.reshape(1, -1)
            similarities = np.dot(db_embeddings, query_embedding.T).squeeze()

            for i, sim in enumerate(similarities):
                if sim >= similarity_threshold:
                    results.append((filepaths[i], scene_indices[i], start_times[i], end_times[i], thumbnails[i], float(sim), source_name))
        results.sort(key=lambda x: x[5], reverse=True)
        return results[:top_k]

def search_scenes(query_embedding: np.ndarray, db_paths: List[str], top_k: int = 50, similarity_threshold: float = -1.0, batch_size: int = 1000) -> List[Tuple[str, int, int, int, bytes, float, str]]:
    all_results = []
    for db_path in db_paths:
        if not os.path.exists(db_path):
            continue
        source_name = os.path.basename(db_path)
        db_results = _search_scenes_single(query_embedding, db_path, source_name, top_k, similarity_threshold, batch_size)
        all_results.extend(db_results)
    
    all_results.sort(key=lambda x: x[5], reverse=True)
    
    seen = set()
    deduped = []
    for result in all_results:
        key = (result[0], result[1])
        if key not in seen:
            seen.add(key)
            deduped.append(result)
    
    return deduped[:top_k]

def migrate_database(db_path: str):
    with sqlite3.connect(db_path) as conn:
        # Get the current version of the loaded file
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # VERSION 1: Added thumbnail blob
        if current_version < 1:
            try:
                conn.execute("ALTER TABLE scene_embeddings ADD COLUMN thumbnail BLOB")
            except sqlite3.OperationalError:
                pass
            
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

        # VERSION 2: Added index_queue table
        if current_version < 2:
            try:
                conn.execute("""CREATE TABLE IF NOT EXISTS index_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    is_directory BOOLEAN NOT NULL DEFAULT 0,
                    recursive BOOLEAN NOT NULL DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""")
            except sqlite3.OperationalError:
                pass
            
            conn.execute("PRAGMA user_version = 2")
            conn.commit()


def add_to_queue(db_path: str, path: str, is_directory: bool, recursive: bool = True) -> bool:
    """Add a path to the index queue. Returns True if added, False if already exists."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('INSERT OR IGNORE INTO index_queue (path, is_directory, recursive) VALUES (?, ?, ?)',
                        (str(path), is_directory, recursive))
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def remove_from_queue(db_path: str, item_id: int) -> bool:
    """Remove an item from the index queue by its ID."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('DELETE FROM index_queue WHERE id = ?', (item_id,))
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def clear_queue(db_path: str) -> bool:
    """Clear all items from the index queue."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('DELETE FROM index_queue')
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def get_queue(db_path: str) -> list:
    """Get all items from the index queue, ordered by addition order (id).
    Returns list of tuples: (id, path, is_directory, recursive)
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute('SELECT id, path, is_directory, recursive FROM index_queue ORDER BY id')
            return cursor.fetchall()
    except sqlite3.Error:
        return []


def update_queue_recursive(db_path: str, item_id: int, recursive: bool) -> bool:
    """Update the recursive flag for an item in the index queue."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('UPDATE index_queue SET recursive = ? WHERE id = ?', (recursive, item_id))
            conn.commit()
            return True
    except sqlite3.Error:
        return False


def queue_count(db_path: str) -> int:
    """Return the number of items in the index queue."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM index_queue')
            return cursor.fetchone()[0]
    except sqlite3.Error:
        return 0

def combine_databases(source_db_paths: List[str], output_db_path: str, progress_callback: Optional[Callable] = None) -> None:
    """Merges databases using Python-level iteration to bypass SQLite ATTACH locks."""
    abs_out = str(Path(output_db_path).resolve())
    safe_sources = [p for p in source_db_paths if str(Path(p).resolve()) != abs_out]
    
    init_db(output_db_path)
    
    # Track inserted files to prevent duplicate entries if databases overlap
    seen_images = set()
    seen_videos = set()
    
    with sqlite3.connect(output_db_path, timeout=10.0) as target_conn:
        target_cursor = target_conn.cursor()
        
        for i, source_db in enumerate(safe_sources):
            if progress_callback:
                progress_callback(f"Merging database {i+1}/{len(safe_sources)}...")
                
            try:
                # Connect individually (avoids ATTACH locking completely)
                source_uri = f"file:{Path(source_db).as_posix()}?mode=ro"
                with sqlite3.connect(source_uri, uri=True, timeout=10.0) as source_conn:
                    source_cursor = source_conn.cursor()
                    
                    # 1. Merge image_embeddings
                    source_cursor.execute("SELECT filepath, modified_at, embedding, model_version, file_type FROM image_embeddings")
                    while True:
                        batch = source_cursor.fetchmany(1000)
                        if not batch: break
                        
                        filtered_batch = [row for row in batch if row[0] not in seen_images]
                        if filtered_batch:
                            target_cursor.executemany("""
                                INSERT INTO image_embeddings 
                                (filepath, modified_at, embedding, model_version, file_type) 
                                VALUES (?, ?, ?, ?, ?)
                            """, filtered_batch)
                            seen_images.update(row[0] for row in filtered_batch)
                            
                    # 2. Merge processed_videos & scene_embeddings
                    source_cursor.execute("SELECT filepath, modified_at, model_version FROM processed_videos")
                    while True:
                        v_batch = source_cursor.fetchmany(500)
                        if not v_batch: break
                        
                        filtered_v_batch = [row for row in v_batch if row[0] not in seen_videos]
                        if not filtered_v_batch:
                            continue
                            
                        # Insert videos
                        target_cursor.executemany("""
                            INSERT INTO processed_videos 
                            (filepath, modified_at, model_version) 
                            VALUES (?, ?, ?)
                        """, filtered_v_batch)
                        
                        new_filepaths = [row[0] for row in filtered_v_batch]
                        seen_videos.update(new_filepaths)
                        
                        # Fetch and insert scenes linked to these videos
                        placeholders = ','.join('?' * len(new_filepaths))
                        source_cursor.execute(f"""
                            SELECT filepath, scene_index, start_time_ms, end_time_ms, embedding, thumbnail 
                            FROM scene_embeddings 
                            WHERE filepath IN ({placeholders})
                        """, new_filepaths)
                        
                        scenes = source_cursor.fetchall()
                        if scenes:
                            target_cursor.executemany("""
                                INSERT INTO scene_embeddings 
                                (filepath, scene_index, start_time_ms, end_time_ms, embedding, thumbnail) 
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, scenes)
                            
            except Exception as e:
                raise RuntimeError(f"Error reading {os.path.basename(source_db)}: {e}")
                
        target_conn.commit()


def get_db_stats(db_path: str) -> dict:
    """Return metadata about a database: scene_count, video_count, image_count, file_size_kb."""
    stats = {'scene_count': 0, 'video_count': 0, 'image_count': 0, 'file_size_kb': 0}
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM scene_embeddings')
            stats['scene_count'] = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM processed_videos')
            stats['video_count'] = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM image_embeddings')
            stats['image_count'] = cursor.fetchone()[0]
        if os.path.exists(db_path):
            stats['file_size_kb'] = round(os.path.getsize(db_path) / 1024, 1)
    except sqlite3.Error:
        pass
    return stats