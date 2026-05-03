import os
import io
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

import av
av.logging.set_level(av.logging.PANIC)

import numpy as np
import torch

from PIL import Image
from scenedetect import detect
from scenedetect.detectors import AdaptiveDetector
from tqdm import tqdm

import config
from database import cleanup_orphaned_entries
from utils import normalize_embedding


def _run_batch_inference(frames, info, model, processor, device, cursor, path, generate_thumbnails, pbar=None):
    """Internal helper to handle SigLIP2 inference and DB insertion."""
    if not frames:
        return

    try:
        inputs = processor(images=frames, return_tensors='pt').to(device)
        
        with torch.no_grad():
            output = model.get_image_features(**inputs)
            features = output.pooler_output if hasattr(output, 'pooler_output') else output[0]
            embeddings = features.cpu().numpy().astype(np.float32)
        for idx, (scene_idx, start_ms, end_ms) in enumerate(info):
            emb_bytes = embeddings[idx].tobytes()
            thumb_bytes = None

            if generate_thumbnails:
                thumb = frames[idx].copy()
                thumb.thumbnail((160, 160), Image.Resampling.BILINEAR)
                buffer = io.BytesIO()
                thumb.save(buffer, format="JPEG", quality=60, optimize=True)
                thumb_bytes = buffer.getvalue()

            cursor.execute('''
                INSERT INTO scene_embeddings 
                (filepath, scene_index, start_time_ms, end_time_ms, embedding, thumbnail) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (path, scene_idx, start_ms, end_ms, emb_bytes, thumb_bytes))
        
        if pbar:
            pbar.update(len(info))
    except Exception as e:
        tqdm.write(f"Inference Error: {e}")

def fast_process_and_embed(video_path, model, processor, device, cursor, generate_thumbnails, batch_size=16, cancel_event=None):
    tqdm.write(f"Fast processing: {os.path.basename(video_path)}")
    
    try:
        # Use options to ignore errors in the container header
        container = av.open(video_path, options={'err_detect': 'ignore_err'})
        video_stream = container.streams.video[0]
        
        # Disable multi-threading to prevent Picture Order Count (POC) collisions
        video_stream.thread_type = "NONE" 
        
        # Force the decoder to only bother with keyframes at the hardware level
        video_stream.codec_context.skip_frame = 'NONKEY'
        
        total_duration_ms = int((container.duration / av.time_base) * 1000)
        
        # Calculate a safe margin (~2 frames) to prevent player bleeding into the next scene
        fps = float(video_stream.average_rate) if video_stream.average_rate and video_stream.average_rate > 0 else 30.0
        safe_margin_ms = int((1000 / fps) * 2)

        pbar = tqdm(total=total_duration_ms / 60000, position=1, unit='min', unit_scale=True, 
                    desc=f'Progress video')
        
        batch_frames = []
        batch_info = []
        pending_frame = None
        pending_start_ms = None
        scene_idx = 0
        last_pbar_update = 0

        # METHOD: decode() is more robust than demux() for corrupted interleaving
        for frame in container.decode(video=0):
            if cancel_event and cancel_event.is_set():
                container.close()
                pbar.close()
                return False
            
            try:
                if not frame.key_frame:
                    continue

                current_ms = int(frame.time * 1000)
                
                if pending_frame is not None:
                    batch_frames.append(pending_frame)
                    # Apply the safe margin, ensuring we don't go below the start time
                    e_ms = max(pending_start_ms + 10, current_ms - safe_margin_ms)
                    batch_info.append((scene_idx, pending_start_ms, e_ms))
                    scene_idx += 1
                
                pending_frame = Image.fromarray(frame.to_ndarray(format='rgb24'))
                pending_start_ms = current_ms

                if len(batch_frames) >= batch_size:
                    _run_batch_inference(batch_frames, batch_info, model, processor, device, cursor, video_path, generate_thumbnails=generate_thumbnails)
                    pbar.update((batch_info[-1][2] - last_pbar_update) / 60000)
                    last_pbar_update = batch_info[-1][2]
                    batch_frames, batch_info = [], []

            except (av.AVError, ValueError) as e:
                # This catches the "Duplicate POC" at the frame level and skips it
                continue

        # Handle last keyframe
        if pending_frame is not None:
            batch_frames.append(pending_frame)
            batch_info.append((scene_idx, pending_start_ms, total_duration_ms))

        if batch_frames:
            _run_batch_inference(batch_frames, batch_info, model, processor, device, cursor, video_path, generate_thumbnails=generate_thumbnails)
            pbar.update((batch_info[-1][2] - last_pbar_update) / 60000)

        container.close()
        pbar.close()
        return True

    except Exception as e:
        tqdm.write(f"Fatal error: {e}")
        return False

def accurate_process_and_embed(video_path, model, processor, device, cursor, generate_thumbnails, batch_size=16, cancel_event=None,):
    """
    Two-pass accurate detection:
    1. PySceneDetect finds precise cut points (slower, looks at every frame).
    2. PyAV streams and extracts those specific frames for embedding.
    """
    tqdm.write(f"Accurate processing: {os.path.basename(video_path)}")
    
    # --- STAGE 1: Accurate Scene Detection ---
    # Threshold 27.0 is usually standard for 'ContentDetector'
    detector = AdaptiveDetector(adaptive_threshold=3.0)
    scene_list = detect(video_path, detector, show_progress=True)
    
    if not scene_list:
        return False

    # Convert scene_list (start_time, end_time) to a dictionary for lookup
    scene_map = {}
    for i, (start, end) in enumerate(scene_list):
        s_ms = int(start.get_seconds() * 1000)
        
        # Calculate a safe margin (~2 frames) using PySceneDetect's framerate data
        fps = float(end.framerate) if end.framerate and end.framerate > 0 else 30.0
        safe_margin_ms = int((1000 / fps) * 2)
        
        e_ms = max(s_ms + 10, int(end.get_seconds() * 1000) - safe_margin_ms)
        scene_map[s_ms] = (i, e_ms)    
    target_start_times = sorted(scene_map.keys())

    pbar = tqdm(total=len(scene_list), position=1, desc=f'Scenes processed:')

    # --- STAGE 2: Efficient Extraction & Embedding ---
    container = av.open(video_path)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"

    batch_frames = []
    batch_info = []
    target_idx = 0

    try:
        # Decode only until we hit our last target scene start
        for frame in container.decode(stream):
            if cancel_event and cancel_event.is_set():
                container.close()
                pbar.close()
                return False
            
            if target_idx >= len(target_start_times):
                break
                
            current_ms = int(frame.time * 1000)
            target_ms = target_start_times[target_idx]

            # If the target timestamp is reached or passed
            if current_ms >= target_ms:
                scene_idx, end_ms = scene_map[target_ms]
                
                # Convert to PIL and add to batch
                img = Image.fromarray(frame.to_ndarray(format='rgb24'))
                batch_frames.append(img)
                batch_info.append((scene_idx, target_ms, end_ms))
                
                target_idx += 1

                if len(batch_frames) >= batch_size:
                    _run_batch_inference(batch_frames, batch_info, model, processor, device, cursor, video_path, pbar, generate_thumbnails=generate_thumbnails)
                    batch_frames, batch_info = [], []

        # Cleanup final batch
        if batch_frames:
            _run_batch_inference(batch_frames, batch_info, model, processor, device, cursor, video_path, pbar, generate_thumbnails=generate_thumbnails)

    finally:
        container.close()
        pbar.close()

    return True

def index_files(folder_path: str, device: torch.device, processor, model, db_path: str, batch_size: int=16, generate_thumbnails: bool=True, progress_callback: Optional[Callable]=None, max_num_patches: int=256, video_frames: int=5, downscale_height: int=480, fast_scene_detect: bool=True, toggle_preview_callback: Optional[Callable]=None, cancel_event: Optional[threading.Event] = None) -> str:
    """
    Index files in the given folder. Returns 'completed', 'cancelled', or 'error'.
    """
    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()
    
    if progress_callback:
        progress_callback('Cleaning database of deleted files...')
    cleanup_orphaned_entries(db_path, progress_callback)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    all_files = []
    for ext in config.IMAGE_EXTENSIONS:
        all_files.extend(Path(folder_path).rglob(f'*{ext}'))
    for ext in config.VIDEO_EXTENSIONS:
        all_files.extend(Path(folder_path).rglob(f'*{ext}'))
    paths_to_process = []
    if progress_callback:
        progress_callback(f'Checking {len(all_files)} files...')
    for path_obj in tqdm(all_files, desc='Checking file modification times'):
        path = str(path_obj)
        try:
            last_modified = os.path.getmtime(path)
            if path.lower().endswith(config.IMAGE_EXTENSIONS):
                cursor.execute('SELECT modified_at FROM image_embeddings WHERE filepath=?', (path,))
            elif path.lower().endswith(config.VIDEO_EXTENSIONS):
                # retrieve model_version for potential future checks
                cursor.execute('SELECT modified_at, model_version FROM processed_videos WHERE filepath=?', (path,))
            else:
                # skip any other type
                continue
            result = cursor.fetchone()
            if not result or result[0] < last_modified:
                paths_to_process.append(path)
        except FileNotFoundError:
            continue
    if not paths_to_process:
        if progress_callback:
            progress_callback('Database is up to date.')
        conn.close()
        return 'completed'
    images_to_process = [p for p in paths_to_process if p.lower().endswith(config.IMAGE_EXTENSIONS)]
    videos_to_process = [p for p in paths_to_process if p.lower().endswith(config.VIDEO_EXTENSIONS)]

    processed_count = 0
    total_to_process = len(paths_to_process)
    if(total_to_process > 0):
        # Only enable scene playback in GUI mode (when progress_callback is provided)
        if config.SCENE_PLAYBACK and progress_callback is not None and toggle_preview_callback is not None:
            toggle_preview_callback()
    if images_to_process:
        for i in tqdm(range(0, len(images_to_process), batch_size), desc='Processing image batches'):
            batch_paths = images_to_process[i:i + batch_size]
            batch_images, valid_paths, mtimes = ([], [], [])
            for path in batch_paths:
                if progress_callback:
                    progress_callback({
                        "current": processed_count,
                        "total": total_to_process,
                        "file": f"Batch of {len(valid_paths)} images",
                        "type": "image"
                    })

                try:
                    batch_images.append(Image.open(path).convert('RGB'))
                    valid_paths.append(path)
                    mtimes.append(os.path.getmtime(path))
                except Exception as e:
                    print(f'Error loading image {path}: {e}')
            if not batch_images:
                continue
            try:
                inputs = processor(images=batch_images, return_tensors='pt', max_num_patches=max_num_patches).to(device)
                with torch.no_grad():
                    output = model.get_image_features(**inputs)
                    image_features = output.pooler_output if hasattr(output, 'pooler_output') else output[0]
                    image_features = normalize_embedding(image_features)
                for idx, (path, mtime) in enumerate(zip(valid_paths, mtimes)):
                    embedding = image_features[idx].cpu().numpy().astype(np.float32).tobytes()
                    cursor.execute('REPLACE INTO image_embeddings (filepath, modified_at, embedding, file_type) VALUES (?, ?, ?, ?)', (path, mtime, embedding, 'image'))
                processed_count += len(valid_paths)
                if progress_callback:
                    progress_callback(f'Indexing: {processed_count}/{total_to_process}')
            except Exception as e:
                print(f'Error processing image batch: {e}')
            conn.commit()
            if is_cancelled():
                conn.close()
                return 'cancelled'
    if videos_to_process:
        print("Starting to index videos, this can take a little while...")
        # outer progress bar for videos
        for path in tqdm(videos_to_process, desc='Total videos', position=0):
            if is_cancelled():
                conn.close()
                return 'cancelled'
            if progress_callback:
                progress_callback({
                    "current": processed_count + 1,
                    "total": total_to_process,
                    "file": os.path.basename(path)
                })
            try:
                cursor.execute('DELETE FROM scene_embeddings WHERE filepath = ?', (path,))
                video_processed = False
                if fast_scene_detect:
                    video_processed = fast_process_and_embed(path, model, processor, device, cursor, batch_size=batch_size, cancel_event=cancel_event, generate_thumbnails=generate_thumbnails)
                else:
                    video_processed = accurate_process_and_embed(path, model, processor, device, cursor, batch_size=batch_size, cancel_event=cancel_event, generate_thumbnails=generate_thumbnails)

                if not video_processed:
                    # Video processing was cancelled or failed
                    if cancel_event and cancel_event.is_set():
                        conn.rollback()
                        conn.close()
                        return 'cancelled'
                    continue

                # mark video as processed
                try:
                    mtime = os.path.getmtime(path)
                    cursor.execute('REPLACE INTO processed_videos (filepath, modified_at, model_version) VALUES (?, ?, ?)', (path, mtime, config.DEFAULT_MODEL))
                except Exception:
                    pass
                processed_count += 1
            except Exception as e:
                print(f'Error processing video {path}: {e}')
            conn.commit()
        conn.close()
    if progress_callback:
        progress_callback(f'Indexing complete. Processed {total_to_process} new/modified files.')
    return 'completed'

def get_query_embedding(query_text: str, query_image_path: Optional[str], device: torch.device, processor, model, max_num_patches: int=256) -> Optional[np.ndarray]:
    text_embedding, image_embedding = (None, None)
    with torch.no_grad():
        if query_text:
            inputs = processor(text=[query_text.lower()], return_tensors='pt', padding='max_length', max_length=64).to(device)
            text_output = model.get_text_features(**inputs)
            text_features = text_output.pooler_output if hasattr(text_output, 'pooler_output') else text_output[0]
            text_embedding = normalize_embedding(text_features).cpu().numpy().astype(np.float32)
        if query_image_path:
            try:
                image = Image.open(query_image_path).convert('RGB')
                inputs = processor(images=image, return_tensors='pt', max_num_patches=max_num_patches).to(device)
                output = model.get_image_features(**inputs)
                image_features = output.pooler_output if hasattr(output, 'pooler_output') else output[0]
                image_embedding = normalize_embedding(image_features).cpu().numpy().astype(np.float32)
            except Exception as e:
                print(f'Error processing query image: {e}')
                return None
    if text_embedding is not None and image_embedding is not None:
        return (text_embedding + image_embedding) / 2
    return text_embedding if text_embedding is not None else image_embedding
