import json
import os
import warnings
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image

Image.MAX_IMAGE_PIXELS = None
warnings.filterwarnings('ignore', category=Image.DecompressionBombWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_FOLDER = PROJECT_ROOT / "temp"
CONFIG_FILE = PROJECT_ROOT / "scene_scout_config.json"
ASSETS_DIR = PROJECT_ROOT / "assets"
THEMES_DIR = ASSETS_DIR / "themes"

big_logo = ASSETS_DIR / "logo" / "scene-scout-logo.png"
text_logo = ASSETS_DIR / "logo" / "scene-scout-text-logo.png"

DEFAULT_MODEL = 'google/siglip2-so400m-patch16-naflex'
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.ts', '.m2ts', '.mts', '.mpg', '.mpeg', '.vob', '.m4v', '.f4v', '.3gp', '.ogv', '.mxf')

# Defining every possible setting and its baseline value
DEFAULT_CONFIG = {
    "generate_thumbnails": True,
    "scene_playback": True,
    "theme": "dark_lightgreen.xml",
    "use_trt": False,
    "use_vlc_open": True,
    "device": None,
    "top_k": 20,
    "batch_size": 16,
    "fast_detect": True,
    "max_patches": 256,
    "frames_per_scene": 3,
    "active_databases": [],
    "primary_database": "",
    "github_token": "",
    "hf_token": "",
    "show_update_details": False,
    "export_mode": "encode",
    "export_audio_mode": "Copy Audio (Fast)",
    "export_video_codec": "H.264 (libx264)",
    "export_audio_codec": "AAC (aac)",
    "export_crf": 23,
    "export_audio_bitrate": "192k",
    "export_open_folder": True,
    "export_container": "MP4 (.mp4)",
    "naming_template": "{source-name}_scene_{time-start}",
    "gpu_standby": True,
    "idle_offload_seconds": 300
}

def load_config() -> Dict[str, Any]:
    # Start with a fresh copy of defaults
    current_config = DEFAULT_CONFIG.copy()
    
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved_data = json.load(f)
                # Overwrite defaults with whatever is in the file
                current_config.update(saved_data)
                
            # Migration: convert old db_path → active_databases + primary_database
            if 'db_path' in current_config:
                old_db_path = current_config.pop('db_path')
                if old_db_path and os.path.exists(old_db_path):
                    abs_path = str(Path(old_db_path).resolve())
                    if abs_path not in current_config['active_databases']:
                        current_config['active_databases'].append(abs_path)
                    if not current_config['primary_database']:
                        current_config['primary_database'] = abs_path
                    save_config(current_config)
            
            # Cleanup legacy folder_path
            if 'folder_path' in current_config:
                current_config.pop('folder_path')
                save_config(current_config)
                
        except (json.JSONDecodeError, IOError) as e:
            print(f'Error loading config file: {e}')
            
    return current_config

def save_config(config: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except IOError as e:
        print(f'Error saving config file: {e}')

def get_vlc_args():
    """Returns platform-specific VLC initialization flags."""
    args = [
        '--ignore-config',
        '--quiet', 
        '--no-audio', 
        '--no-sub-autodetect-file', 
        '--no-osd',       
        '--no-spu',       
        '--no-stats',     
        '--no-video-title-show'
    ]
    
    if sys.platform == 'darwin':
        # Required for rendering in a Cocoa-based container on macOS
        args.append('--vout=macosx')
    elif sys.platform.startswith('linux'):
        # Prevents X11 threading issues on Linux
        args.append('--no-xlib')
        
    return args

def get_hf_token() -> Optional[str]:
    """Retrieves the HF token, returning None if not found or empty."""
    token = os.environ.get("HF_TOKEN")
    
    if not token:
        current_config = load_config()
        token = current_config.get("hf_token", "")
    
    if not token or not str(token).strip():
        return None
        
    token = token.strip()
    
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
        
    return token