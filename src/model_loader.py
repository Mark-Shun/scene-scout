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

def get_compute_device(device_choice="cpu"):
    """Determines the best available device and returns (device, dtype)."""
    if device_choice == 'cuda' and torch.cuda.is_available():
        return torch.device('cuda'), torch.float16
    elif device_choice == 'rocm' and torch.cuda.is_available():
        return torch.device('cuda'), torch.float16
    elif device_choice == 'xpu' and (hasattr(torch, 'xpu') and torch.xpu.is_available()):
        return torch.device('xpu'), torch.float16
    elif device_choice == 'dml' and torch_directml is not None:
        return torch_directml.device(), torch.float32
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return torch.device('mps'), torch.float32
    return torch.device('cpu'), torch.float32

def load_siglip_model(device_choice="cpu", status_callback=None):
    """Initializes the processor and model[cite: 20, 21]."""
    device, dtype = get_compute_device(device_choice)
    
    def update(msg):
        if status_callback:
            status_callback(msg)

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
    return model, processor, device, dtype