import argparse
import os
import sys
import torch
from transformers import AutoProcessor, Siglip2Model

import config
from database import init_db, cleanup_orphaned_entries, search_scenes, db_is_empty
from processing import index_files, get_query_embedding

try:
    import torch_directml
except ImportError:
    torch_directml = None

try:
    import intel_extension_for_pytorch as ipex
except ImportError:
    ipex = None

def cli_mode():
    parser = argparse.ArgumentParser(description='Scene Scout / Video Scene search tool')
    parser.add_argument('--index', type=str, help='Path to the folder to index')
    parser.add_argument('--search-text', type=str, help='Text to search for')
    parser.add_argument('--search-image', type=str, help='Image to search with')
    parser.add_argument('--top-k', type=int, default=10, help='Number of results to return')
    parser.add_argument('--db', type=str, default='siglip2_embeddings.db', help='Database file path')
    parser.add_argument('--device', type=str, choices=['cuda', 'cpu', 'dml'], help='Force a specific device')
    parser.add_argument('--max-patches', type=int, default=256, help='Max patches for the model')
    parser.add_argument('--batch-size', type=int, default=16, help='Batch size for scene embedding')
    parser.add_argument('--accurate', action='store_true', help='Use accurate scene detection instead of fast mode')
    parser.add_argument('--cleanup', action='store_true', help='Clean up orphaned embeddings from the database')
    
    args = parser.parse_args()
    
    device_str, msg = config.setup_device(args.device)
    print(f'Device: {msg}')
    
    # Map the detected/requested string to a torch-compatible device
    if device_str == 'cuda' or device_str == 'rocm':
        device = torch.device('cuda')
        dtype = torch.float16
    elif device_str == 'xpu' and (hasattr(torch, 'xpu') and torch.xpu.is_available()):
        device = torch.device('xpu')
        dtype = torch.float16
    elif device_str == 'dml' and torch_directml:
        device = torch_directml.device()
        dtype = torch.float32
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        device = torch.device('mps')
        dtype = torch.float32
    else:
        # Fallback to CPU if hardware is missing or unsupported
        device = torch.device('cpu')
        dtype = torch.float32
    
    init_db(args.db)
    
    if args.cleanup:
        print('Cleaning up orphaned embeddings...')
        count = cleanup_orphaned_entries(args.db)
        print(f'Removed {count} orphaned embeddings.')
        
    if not (args.index or args.search_text or args.search_image):
        print('No action specified. Use --index, --search-text, or --search-image.')
        return
        
    print(f'Loading model {config.DEFAULT_MODEL}...')
    processor = AutoProcessor.from_pretrained(config.DEFAULT_MODEL)
    attn_implementation = 'sdpa' if hasattr(torch.nn.functional, 'scaled_dot_product_attention') else 'eager'
    model = Siglip2Model.from_pretrained(config.DEFAULT_MODEL, torch_dtype=dtype, attn_implementation=attn_implementation).to(device)
    model.eval()
    print('Model loaded.')
    
    if args.index:
        print(f'\nIndexing folder: {args.index}')
        index_files(
            args.index, 
            device, 
            processor, 
            model, 
            args.db, 
            batch_size=args.batch_size,
            max_num_patches=args.max_patches, 
            fast_scene_detect=not args.accurate
        )
        print('Indexing complete.')
        
    if args.search_text or args.search_image:
        print('\nPerforming search...')
        if db_is_empty(args.db):
            print('Warning: The database appears to be empty. Please index files before searching.')
            return
            
        query_embedding = get_query_embedding(args.search_text, args.search_image, device, processor, model, args.max_patches)
        if query_embedding is None:
            print('Error: Could not generate query embedding.')
            return
            
        results = search_scenes(query_embedding, args.db, top_k=args.top_k)
        
        if not results:
            print('\nNo results found.')
        else:
            print(f'\n--- Top {len(results)} Scene Results ---')
            for i, (path, scene_idx, start_time, end_time, score) in enumerate(results, 1):
                def fmt(ms):
                    mins = ms // 60000
                    secs = (ms % 60000) // 1000
                    msr = ms % 1000
                    # Standardized with your GUI's new hour logic
                    hours = ms // 3600000
                    if hours > 0:
                        return f"{hours}:{mins:02d}:{secs:02d}.{msr:03d}"
                    return f'{mins}:{secs:02d}.{msr:03d}'
                    
                time_str = fmt(start_time)
                if end_time is not None:
                    time_str = f'{time_str}-{fmt(end_time)}'
                print(f'{i:2d}. [Scene {scene_idx+1} @ {time_str}] Score: {score:.4f} | {os.path.basename(path)}')
            print('-' * 20)