import json
import warnings
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image

try:
    import torch
except ImportError:
    torch = None

try:
    import torch_directml
except ImportError:
    torch_directml = None

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

def setup_device(forced_device: Optional[str]=None) -> Tuple[str, str]:
    if forced_device:
        if forced_device == 'dml' and (torch_directml is None or not torch_directml.is_available()):
            return ('cpu', 'DirectML not available. Falling back to CPU.')
        if forced_device == 'cuda' and (torch is None or not torch.cuda.is_available()):
            return ('cpu', 'CUDA not available. Falling back to CPU.')
        return (forced_device, f'Device forced to {forced_device.upper()}.')
    if torch is not None and torch.cuda.is_available():
        return ('cuda', 'NVIDIA GPU (CUDA) detected.')
    if torch_directml and torch_directml.is_available():
        return ('dml', 'AMD/Intel GPU (DirectML) detected.')
    return ('cpu', 'No compatible GPU found. Using CPU.')
