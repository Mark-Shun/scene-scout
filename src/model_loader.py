import torch
from transformers import AutoProcessor, Siglip2Model
import config

# Hardware Backend Imports
try:
    import torch_directml
except ImportError:
    torch_directml = None

try:
    import intel_extension_for_pytorch as ipex
except ImportError:
    ipex = None

def get_compute_device(device_choice=None):
    """
    Determines the best available device.
    Returns (device_str, msg, torch_device, torch_dtype).
    """
    # 1. Handle forced devices
    if device_choice:
        if device_choice == 'dml' and (torch_directml is None or not torch_directml.is_available()):
            return 'cpu', 'DirectML not available. Falling back to CPU.', torch.device('cpu'), torch.float32
        if device_choice == 'cuda' and (torch is None or not torch.cuda.is_available()):
            return 'cpu', 'CUDA not available. Falling back to CPU.', torch.device('cpu'), torch.float32
        
        # If forced and valid, map it
        if device_choice == 'cuda' or device_choice == 'rocm':
            return device_choice, f'Device forced to {device_choice.upper()}.', torch.device('cuda'), torch.float16
        if device_choice == 'dml':
            return 'dml', 'Device forced to DML.', torch_directml.device(), torch.float32
            
    # 2. Auto-detect if not forced
    if torch is not None and torch.cuda.is_available():
        return 'cuda', 'NVIDIA/AMD GPU detected.', torch.device('cuda'), torch.float16
    if hasattr(torch, 'xpu') and torch.xpu.is_available():
        return 'xpu', 'Intel GPU (XPU) detected.', torch.device('xpu'), torch.float16
    if torch_directml and torch_directml.is_available():
        return 'dml', 'AMD/Intel GPU (DirectML) detected.', torch_directml.device(), torch.float32
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps', 'Apple Silicon (MPS) detected.', torch.device('mps'), torch.float32
        
    return 'cpu', 'No compatible GPU found. Using CPU.', torch.device('cpu'), torch.float32

def load_siglip_model(device_choice=None, status_callback=None):
    """Initializes the processor and model."""
    device_str, msg, device, dtype = get_compute_device(device_choice)
    
    def update(text):
        if status_callback:
            status_callback(text)

    update(f"Hardware Status: {msg}")
    update("Loading processor config...")
    processor = AutoProcessor.from_pretrained(config.DEFAULT_MODEL)

    update("Loading model weights (this can take a while)...")
    attn_implementation = 'sdpa' if hasattr(torch.nn.functional, 'scaled_dot_product_attention') else 'eager'
    model = Siglip2Model.from_pretrained(
        config.DEFAULT_MODEL, 
        torch_dtype=dtype, 
        attn_implementation=attn_implementation
    ).to(device)
    
    model.eval()
    return model, processor, device, dtype, device_str