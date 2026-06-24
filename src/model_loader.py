# Scene Scout - Natural language video scene search
# Copyright (C) 2026 Mark-Shun/Sonicfreak1111
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file contains modified code of original work by Gabriele Peris,
# originally released under the MIT License. See LICENSE for details.
import gc
import platform
import sys
import torch
import transformers
from transformers import AutoProcessor, AutoModel
import config
import os
import logging
import warnings

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
    logging.info(f"Hardware Detection - OS: {sys.platform} | Architecture: {platform.machine()}")

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
            msg = f"Older NVIDIA GPU ({device_name}) detected. Using Float32 for compatibility."
        else:
            dtype = torch.float16
            msg = f"Modern NVIDIA GPU ({device_name}) detected. Using Float16 for performance."
            
        return 'cuda', msg, torch.device('cuda'), dtype
    if hasattr(torch, 'xpu') and torch.xpu.is_available():
        return 'xpu', 'Intel GPU (XPU) detected.', torch.device('xpu'), torch.float32
    if torch_directml and torch_directml.is_available():
        return 'dml', 'AMD/Intel GPU (DirectML) detected.', torch_directml.device(), torch.float32
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps', 'Apple Silicon (MPS) detected.', torch.device('mps'), torch.float32
        
    return 'cpu', 'No compatible GPU found. Using CPU.', torch.device('cpu'), torch.float32

def unload_model(model, processor=None, update_fn=None):
    """
    Safely destroys the model and forces the GPU to release the VRAM.
    Can be used by the GUI for dynamic reloading or the CLI for batch cleanup.
    """
    import torch
    import gc

    if model is not None:
        del model
    if processor is not None:
        del processor

    if hasattr(torch, '_dynamo'):
        torch._dynamo.reset()

    gc.collect()
    gc.collect()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        if hasattr(torch.cuda, 'ipc_collect'):
            torch.cuda.ipc_collect()

    if update_fn:
        update_fn("Model and compiler caches unloaded from VRAM.")

    return None, None

def load_siglip_model(device_choice=None, status_callback=None, use_trt=False):
    """Initializes the processor and model with Zero-Copy VRAM loading."""
    device_str, msg, device, dtype = get_compute_device(device_choice)

    def update(text):
        if status_callback:
            status_callback(text)

    update(f"Hardware Status: {msg}")

    logging.info(f"Target Device: {device_str.upper()} | Selected Precision: {dtype}")
    logging.info(f"Target Model: {config.DEFAULT_MODEL}")
    logging.info(f"Transformers Version: {transformers.__version__}")
    logging.info(f"Attention Implementation: {config.ATTENTION_IMPL}")

    attn_impl = config.ATTENTION_IMPL

    cached = _is_model_cached(config.DEFAULT_MODEL)

    update("Loading processor config...")
    processor = AutoProcessor.from_pretrained(config.DEFAULT_MODEL, token=config.get_hf_token())
    update("Processor loaded.")

    if cached:
        update(f"Loading weights directly to VRAM via accelerate...")
    else:
        update(f"Downloading model weights...") 

    if device_str == 'cpu':
        try:
            import psutil
            physical_cores = psutil.cpu_count(logical=False)
            if physical_cores is None:
                physical_cores = os.cpu_count()
            torch.set_num_threads(physical_cores)
            update(f"Optimized CPU threads to {physical_cores} physical cores.")
            logging.info(f"CPU Optimization: Restricted to {physical_cores} physical cores via psutil.")
        except ImportError:
            try:
                if platform.machine().lower() in ['x86_64', 'amd64']:
                    physical_cores = max(1, os.cpu_count() // 2)
                else:
                    physical_cores = os.cpu_count()
                torch.set_num_threads(physical_cores)
                update(f"Optimized CPU threads to {physical_cores} physical cores.")
                logging.info(f"CPU Optimization: Restricted to {physical_cores} physical cores via os limit.")
            except Exception as e:
                logging.warning(f"Failed to optimize CPU threads: {e}")
        except Exception as e:
            logging.warning(f"Failed to optimize CPU threads: {e}")

    model_kwargs = {
        "pretrained_model_name_or_path": config.DEFAULT_MODEL,
        "token": config.get_hf_token(),
        "dtype": dtype,
        "attn_implementation": attn_impl,
    }

    if device_str == 'cuda':
        model_kwargs["device_map"] = {"": torch.cuda.current_device()}
    elif device_str in ['mps', 'cpu', 'xpu']:
        model_kwargs["device_map"] = {"": device_str}

    model = AutoModel.from_pretrained(**model_kwargs)
    update(f"Model loaded on {device_str}.")

    model.requires_grad_(False)

    if use_trt and device_str == 'cuda' and TRT_AVAILABLE:
        update("Applying TorchDynamo TensorRT optimization...")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                logging.getLogger("torch_tensorrt").setLevel(logging.ERROR)
                torch._dynamo.config.suppress_errors = True

            model.vision_model = torch.compile(
                model.vision_model,
                backend="tensorrt",
                dynamic=True
            )
            update("Compiling TensorRT engine (this may take a minute)...")

            from PIL import Image
            dummy_image = Image.new('RGB', (224, 224), color='black')
            current_config = config.load_config()
            max_patches = current_config.get('max_patches', 256)

            dummy_inputs = processor(images=dummy_image, return_tensors="pt", max_num_patches=max_patches).to(device)

            with torch.no_grad():
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    model.get_image_features(**dummy_inputs)

            update("TensorRT JIT Compiler Active and Warmed Up!")
        except (Exception, AttributeError) as e:
            update(f"TensorRT Compilation failed ({e}). Falling back to standard CUDA.")

    model.eval()
    return model, processor, device, dtype, device_str