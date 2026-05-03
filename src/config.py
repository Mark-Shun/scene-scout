import json
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

big_logo = ASSETS_DIR / "logo" / "scene-scout-logo.png"
text_logo = ASSETS_DIR / "logo" / "scene-scout-text-logo.png"

DEFAULT_MODEL = 'google/siglip2-so400m-patch16-naflex'
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm')

SCENE_DETECTOR_THRESHOLD = 30

# Defining every possible setting and its baseline value
DEFAULT_CONFIG = {
    "generate_thumbnails": True,
    "scene_playback": True,
    "theme": "radiance",
    "use_trt": False,
    "use_vlc_open": True,
    "device": None,
    "top_k": 20,
    "batch_size": 16,
    "fast_detect": True,
    "max_patches": 256,
    "folder_path": "",
    "db_path": ""
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
