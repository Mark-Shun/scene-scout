import torch
from transformers import AutoProcessor, Siglip2Model
import config
import os

# Hardware Backend Imports
try:
    import torch_directml
except ImportError:
    torch_directml = None

try:
    import intel_extension_for_pytorch as ipex
except ImportError:
    ipex = None

try:
    import torch_tensorrt
    TRT_AVAILABLE = True
except ImportError:
    torch_tensorrt = None
    TRT_AVAILABLE = False

from huggingface_hub import snapshot_download

# Define a path for the cached engine
ENGINE_CACHE_PATH = os.path.join(os.path.dirname(__file__), "../", "siglip2_trt_engine.ts")

def _is_model_cached(model_id: str) -> bool:
    try:
        snapshot_download(model_id, local_files_only=True)
        return True
    except Exception:
        return False

def get_compute_device(device_choice=None):
    """
    Determines the best available device and optimal precision.
    Returns (device_str, msg, torch_device, torch_dtype).
    """
    # 1. Handle forced devices
    if device_choice:
        if device_choice == 'dml' and (torch_directml is None or not torch_directml.is_available()):
            return 'cpu', 'DirectML not available. Falling back to CPU.', torch.device('cpu'), torch.float32
        if device_choice == 'cuda' and (torch is None or not torch.cuda.is_available()):
            return 'cpu', 'CUDA not available. Falling back to CPU.', torch.device('cpu'), torch.float32
            
    # 2. Auto-detect and handle Architecture-specific Precision
    if torch is not None and torch.cuda.is_available():
        major, minor = torch.cuda.get_device_capability()
        device_name = torch.cuda.get_device_name()
        
        # Pascal (6.x) and older perform poorly with FP16 or lack hardware support.
        # Volta (7.0), Turing (7.5), and newer have Tensor Cores for fast FP16.
        if major < 7:
            dtype = torch.float32
            msg = f"NVIDIA GPU ({device_name})."
        else:
            dtype = torch.float16
            msg = f"NVIDIA GPU ({device_name})."
            
        return 'cuda', msg, torch.device('cuda'), dtype
    if hasattr(torch, 'xpu') and torch.xpu.is_available():
        return 'xpu', 'Intel GPU (XPU) detected.', torch.device('xpu'), torch.float16
    if torch_directml and torch_directml.is_available():
        return 'dml', 'AMD/Intel GPU (DirectML) detected.', torch_directml.device(), torch.float32
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps', 'Apple Silicon (MPS) detected.', torch.device('mps'), torch.float32
        
    return 'cpu', 'No compatible GPU found. Using CPU.', torch.device('cpu'), torch.float32

def load_siglip_model(device_choice=None, status_callback=None, use_trt=False):
    """Initializes the processor and model."""
    device_str, msg, device, dtype = get_compute_device(device_choice)

    def update(text):
        if status_callback:
            status_callback(text)

    update(f"Hardware Status: {msg}")

    model_cached = _is_model_cached(config.DEFAULT_MODEL)

    update("Loading processor config..." if model_cached else "Downloading processor config... (first run)")
    processor = AutoProcessor.from_pretrained(config.DEFAULT_MODEL, token=config.get_hf_token())

    update(f"Loading weights in {str(dtype).split('.')[-1]}..." if model_cached else f"Downloading weights ({str(dtype).split('.')[-1]})... (first run)")
    model = Siglip2Model.from_pretrained(
            config.DEFAULT_MODEL, 
            token=config.get_hf_token(), 
            torch_dtype=dtype, 
            attn_implementation='sdpa' if hasattr(torch.nn.functional, 'scaled_dot_product_attention') else 'eager'
        ).to(device)

    if use_trt and device_str == 'cuda' and TRT_AVAILABLE:
        import logging
        import warnings
        update("Applying TorchDynamo TensorRT optimization...")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                logging.getLogger("torch_tensorrt").setLevel(logging.ERROR)
                torch._dynamo.config.suppress_errors = True
                
            # Dynamo natively handles Hugging Face kwargs and dynamic batch sizes,
            # while still utilizing the TensorRT backend for massive speedups.
            model.vision_model = torch.compile(
                model.vision_model, 
                backend="tensorrt", 
                dynamic=True
            )
            update("TensorRT JIT Compiler Active! (Optimization runs on first search)")
        except Exception as e:
            update(f"TensorRT Compilation Error: {e}. Falling back to standard CUDA.")
    
    model.eval()
    return model, processor, device, dtype, device_str