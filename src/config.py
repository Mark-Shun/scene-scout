import json
import warnings
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image

Image.MAX_IMAGE_PIXELS = None
warnings.filterwarnings('ignore', category=Image.DecompressionBombWarning)

CONFIG_FILE = Path(__file__).parent / 'siglip2_config.json'
DEFAULT_MODEL = 'google/siglip2-so400m-patch16-naflex'
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm')

SCENE_DETECTOR_THRESHOLD = 30
SCENE_PLAYBACK = True

def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f'Error loading config file: {e}')
    return {}

def save_config(config: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except IOError as e:
        print(f'Error saving config file: {e}')
