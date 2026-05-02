import argparse
import os
import sys
import cmd
import shlex
import json

import config
from database import init_db, cleanup_orphaned_entries, search_scenes, db_is_empty

# --- Helper Functions ---
def format_time(ms):
    hours = ms // 3600000
    mins = (ms % 3600000) // 60000
    secs = (ms % 60000) // 1000
    msr = ms % 1000
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}.{msr:03d}"
    return f'{mins}:{secs:02d}.{msr:03d}'

def display_results(results, as_json=False):
    """Prints search results to the terminal in text or JSON format."""
    if not results:
        if as_json:
            print(json.dumps([]))
        else:
            print('\nNo results found.')
        return

    if as_json:
        # Build a structured dictionary for JSON serialization
        json_data = []
        for path, scene_idx, start_time, end_time, score in results:
            json_data.append({
                "filepath": path,
                "filename": os.path.basename(path),
                "scene_index": scene_idx + 1 if scene_idx is not None else None,
                "start_time_ms": start_time,
                "end_time_ms": end_time,
                "score": round(score, 4)
            })
        # Print valid JSON to standard output
        print(json.dumps(json_data, indent=2))
        return

    # Fallback to standard text output
    print(f'\n--- Top {len(results)} Scene Results ---')
    for i, (path, scene_idx, start_time, end_time, score) in enumerate(results, 1):
        time_str = format_time(start_time)
        if end_time is not None:
            time_str = f'{time_str}-{format_time(end_time)}'
        print(f'{i:2d}. [Scene {scene_idx+1} @ {time_str}] Score: {score:.4f} | {os.path.basename(path)}')
    print('-' * 20)

def run_search(text, image, device, proc, model, args):
    from processing import get_query_embedding
    if db_is_empty(args.db):
        print('Warning: The database appears to be empty. Please index files first.')
        return

    query_embedding = get_query_embedding(text, image, device, proc, model, args.max_patches)
    if query_embedding is not None:
        results = search_scenes(query_embedding, args.db, top_k=args.top_k)
        
        # Check if json flag exists and is True
        use_json = getattr(args, 'json', False)
        display_results(results, as_json=use_json)
    else:
        print("Error: Could not generate query embedding.")


# --- Interactive Shell ---
class SceneScoutShell(cmd.Cmd):
    intro = "\nInteractive Mode Active. Type 'help' to list commands. Type 'exit' to quit."
    prompt = 'Scene Scout> '

    def __init__(self, initial_args):
        super().__init__()
        self.state = initial_args
        self.model = None
        self.processor = None
        self.device = None

    def _load_model(self):
        """Lazy-loads the model only when a heavy operation is requested."""
        if self.model is None:
            from model_loader import load_siglip_model
            
            # Check if JSON is requested. If it is, suppress status prints.
            use_json = getattr(self.state, 'json', False)
            def callback(msg):
                if not use_json:
                    print(msg)
                    
            self.model, self.processor, self.device, _, _ = load_siglip_model(
                self.state.device, status_callback=callback
            )

    def do_load_db(self, arg):
        """Load specified database. Usage: load_db <path>"""
        if not arg:
            print("Please provide a path to the database file.")
            return
            
        try:
            parsed_args = shlex.split(arg)
            db_path = parsed_args[0]
        except ValueError as e:
            print(f"Error parsing path: {e}")
            return
            
        try:
            init_db(db_path)
            self.state.db = db_path 
            print(f"Database loaded successfully: {db_path}")
        except Exception as e:
            print(f"Failed to load database: {e}")

    def do_set(self, arg):
        """Set a variable. Usage: set <variable> <value> (e.g., set json true)"""
        args = shlex.split(arg)
        if len(args) != 2:
            print("Usage: set <variable> <value>")
            return
        key, val = args[0], args[1]
        
        # 1. Handle explicit Boolean strings
        val_lower = val.lower()
        if val_lower in ['true', '1', 'yes', 'y']:
            parsed_val = True
        elif val_lower in ['false', '0', 'no', 'n']:
            parsed_val = False
        # 2. Try integer parsing
        elif val.isdigit():
            parsed_val = int(val)
        # 3. Fallback to string
        else:
            parsed_val = val

        # 4. Set the attribute (even if it wasn't strictly defined by argparse)
        setattr(self.state, key, parsed_val)
        print(f"{key} updated to {getattr(self.state, key)}")

    def do_status(self, arg):
        """Show current configuration."""
        print("\n--- Current State ---")
        for k, v in vars(self.state).items():
            print(f"{k}: {v}")
        print(f"Model Loaded: {self.model is not None}\n")

    def do_search(self, arg):
        """Search the database. Usage: search <text or image path>"""
        if not arg:
            print("Please provide a search query.")
            return
        
        self._load_model()
        
        s_image = arg if (os.path.exists(arg) and arg.lower().endswith(config.IMAGE_EXTENSIONS)) else None
        s_text = None if s_image else arg
        run_search(s_text, s_image, self.device, self.processor, self.model, self.state)

    def do_index(self, arg):
        """Index a folder. Usage: index <path/to/folder>"""
        if not arg:
            print("Please provide a folder path.")
            return
            
        init_db(self.state.db)
        self._load_model()
        
        from processing import index_files
        index_files(arg, self.device, self.processor, self.model, self.state.db, 
                    batch_size=self.state.batch_size, max_num_patches=self.state.max_patches, 
                    fast_scene_detect=not self.state.accurate)

    def do_cleanup(self, arg):
        """Clean up orphaned database entries."""
        init_db(self.state.db)
        count = cleanup_orphaned_entries(self.state.db)
        print(f'Removed {count} orphaned embeddings.')

    def do_exit(self, arg):
        """Exit the shell."""
        print("Exiting...")
        return True
        
    def do_quit(self, arg):
        return self.do_exit(arg)


# --- Main CLI Entry ---
def cli_mode():
    parser = argparse.ArgumentParser(description='Scene Scout CLI')
    parser.add_argument('--interactive', action='store_true', help='Enter interactive REPL mode')
    parser.add_argument('--index', type=str, help='Path to folder to index')
    parser.add_argument('--search-text', type=str, help='Text to search for')
    parser.add_argument('--search-image', type=str, help='Image path to search with')
    parser.add_argument('--json', action='store_true', help='Output search results in JSON format')
    parser.add_argument('--top-k', type=int, default=10, help='Results to return')
    parser.add_argument('--db', type=str, default='siglip2_embeddings.db', help='DB path')
    parser.add_argument('--device', type=str, choices=['cuda', 'cpu', 'dml', 'xpu', 'mps'], help='Force device')
    parser.add_argument('--max-patches', type=int, default=256, help='Max model patches')
    parser.add_argument('--batch-size', type=int, default=16, help='Inference batch size')
    parser.add_argument('--accurate', action='store_true', help='Use accurate detection')
    parser.add_argument('--cleanup', action='store_true', help='Clean orphaned entries')
    args = parser.parse_args()

    if args.interactive:
        SceneScoutShell(args).cmdloop()
        return

    # One-shot operations remain identical for backward compatibility
    init_db(args.db)
    if args.cleanup:
        count = cleanup_orphaned_entries(args.db)
        print(f'Removed {count} orphaned embeddings.')
        if not (args.index or args.search_text or args.search_image):
            return

    if not (args.index or args.search_text or args.search_image):
        print('No action specified. Use --help for options.')
        return

    from model_loader import load_siglip_model
    model, processor, device, dtype, _ = load_siglip_model(args.device, status_callback=print)

    if args.index:
        from processing import index_files
        index_files(args.index, device, processor, model, args.db, 
                    batch_size=args.batch_size, max_num_patches=args.max_patches, 
                    fast_scene_detect=not args.accurate)
    
    if args.search_text or args.search_image:
        run_search(args.search_text, args.search_image, device, processor, model, args)