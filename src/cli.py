import argparse
import os
import sys
import cmd
import shlex
import json
import base64

# Cross-platform readline support for command history
try:
    import readline
    HISTORY_AVAILABLE = True
except ImportError:
    try:
        import pyreadline3 as readline
        HISTORY_AVAILABLE = True
    except ImportError:
        readline = None
        HISTORY_AVAILABLE = False

import config
from database import init_db, cleanup_orphaned_entries, search_scenes, db_is_empty

# --- Constants ---
HISTORY_FILE = os.path.expanduser('~/.scene_scout_history')
COMMAND_ALIASES = {
    's': 'search',
    'i': 'index',
    'cl': 'cleanup',
    'ls': 'status',
    'h': 'help',
    'q': 'exit',
}

# --- Helper Functions ---
def format_time(ms):
    hours = ms // 3600000
    mins = (ms % 3600000) // 60000
    secs = (ms % 60000) // 1000
    msr = ms % 1000
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}.{msr:03d}"
    return f'{mins}:{secs:02d}.{msr:03d}'

def display_results(results, as_json=False, include_thumbs=False, output_file=None, silent=False):
    """Prints search results to the terminal in text or JSON format."""
    if not results:
        if as_json:
            json_output = json.dumps([])
            _write_output(json_output, output_file, silent)
        elif not silent:
            print('\nNo results found.')
        return

    if as_json:
        json_data = []
        for path, scene_idx, start_time, end_time, thumb, score in results:
            entry = {
                "filepath": path,
                "filename": os.path.basename(path),
                "scene_index": scene_idx + 1 if scene_idx is not None else None,
                "start_time_ms": start_time,
                "end_time_ms": end_time,
                "score": round(score, 4)
            }
            
            # Encode thumbnail to Base64 if requested and data exists
            if include_thumbs and thumb:
                entry["thumbnail_b64"] = base64.b64encode(thumb).decode('utf-8')
            
            json_data.append(entry)
            
        json_output = json.dumps(json_data, indent=2)
        _write_output(json_output, output_file, silent)
        return

    # Fallback to standard text output
    if silent:
        return
    print(f'\n--- Top {len(results)} Scene Results ---')
    for i, (path, scene_idx, start_time, end_time, thumb, score) in enumerate(results, 1):
        time_str = format_time(start_time)
        if end_time is not None:
            time_str = f'{time_str}-{format_time(end_time)}'
        print(f'{i:2d}. [Scene {scene_idx+1} @ {time_str}] Score: {score:.4f} | {os.path.basename(path)}')
    print('-' * 20)

def _write_output(content, output_file=None, silent=False):
    """Writes output to file or stdout."""
    if output_file:
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(content)
            if not silent:
                print(f'Output written to: {output_file}')
        except IOError as e:
            print(f'Error writing to file: {e}', file=sys.stderr)
            sys.stdout.write(content)
    else:
        sys.stdout.write(content)
        sys.stdout.write('\n')

def run_search(text, image, device, proc, model, args):
    from processing import get_query_embedding
    if db_is_empty(args.db):
        if not getattr(args, 'silent', False):
            print('Warning: The database appears to be empty. Please index files first.')
        return

    query_embedding = get_query_embedding(text, image, device, proc, model, args.max_patches)
    if query_embedding is not None:
        results = search_scenes(query_embedding, args.db, top_k=args.top_k)
        
        # Pull flags from args
        use_json = getattr(args, 'json', False)
        include_thumbs = getattr(args, 'include_thumbs', False)
        output_file = getattr(args, 'output', None)
        is_silent = getattr(args, 'silent', False)
        
        display_results(results, as_json=use_json, include_thumbs=include_thumbs, output_file=output_file, silent=is_silent)
    else:
        if not getattr(args, 'silent', False):
            print("Error: Could not generate query embedding.")


# --- Interactive Shell ---
class SceneScoutShell(cmd.Cmd):
    intro = "\nInteractive Mode Active. Type 'help' to list commands. Type 'exit' to quit."
    prompt = 'Scene Scout> '

    # Known variables for tab completion
    _settable_vars = ['db', 'json', 'include_thumbs', 'top_k', 'device', 'max_patches', 'batch_size', 'accurate', 'silent', 'output']

    def __init__(self, initial_args):
        super().__init__()
        self.state = initial_args
        self.model = None
        self.processor = None
        self.device = None
        self._load_history()

    def _load_history(self):
        """Load command history from file."""
        if not HISTORY_AVAILABLE or readline is None:
            return
        try:
            readline.read_history_file(HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(1000)

    def postcmd(self, stop, line):
        """Save history after each command."""
        if HISTORY_AVAILABLE and readline is not None:
            try:
                readline.write_history_file(HISTORY_FILE)
            except Exception:
                pass
        return stop

    def cmdloop(self, intro=None):
        """Override cmdloop to handle command aliases."""
        try:
            while True:
                try:
                    line = self.cmdloop_line()
                    if line is None:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    # Resolve alias
                    parts = line.split(None, 1)
                    if parts[0] in COMMAND_ALIASES:
                        line = COMMAND_ALIASES[parts[0]]
                        if len(parts) > 1:
                            line += ' ' + parts[1]
                    # Dispatch
                    stop = self.onecmd(line)
                    if stop:
                        break
                except KeyboardInterrupt:
                    if not getattr(self.state, 'silent', False):
                        print()
        finally:
            if HISTORY_AVAILABLE and readline is not None:
                try:
                    readline.write_history_file(HISTORY_FILE)
                except Exception:
                    pass

    def cmdloop_line(self):
        """Read a single line of input."""
        try:
            return input(self.prompt)
        except EOFError:
            print()
            return None

    def _load_model(self):
        """Lazy-loads the model only when a heavy operation is requested."""
        if self.model is None:
            from model_loader import load_siglip_model
            
            # Check if JSON is requested. If it is, suppress status prints.
            use_json = getattr(self.state, 'json', False)
            is_silent = getattr(self.state, 'silent', False)
            def callback(msg):
                if not use_json and not is_silent:
                    print(msg)
                    
            self.model, self.processor, self.device, _, _ = load_siglip_model(
                self.state.device, status_callback=callback
            )

    def complete_set(self, text, line, begidx, endidx):
        """Tab completion for set command variable names."""
        return [v for v in self._settable_vars if v.startswith(text)]

    def do_load_db(self, arg):
        """Load specified database. Usage: load_db <path>"""
        if not arg:
            print("Please provide a path to the database file.")
            return
            
        try:
            parsed_args = shlex.split(arg)
            db_path = os.path.expanduser(parsed_args[0])
        except ValueError as e:
            print(f"Error parsing path: {e}")
            return
            
        try:
            init_db(db_path)
            self.state.db = db_path 
            if not getattr(self.state, 'silent', False):
                print(f"Database loaded successfully: {db_path}")
        except Exception as e:
            print(f"Failed to load database: {e}", file=sys.stderr)
            return 2

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
        if not getattr(self.state, 'silent', False):
            print(f"{key} updated to {getattr(self.state, key)}")

    def do_vars(self, arg):
        """List all editable variables with their current values."""
        print("\n--- Editable Variables ---")
        for v in self._settable_vars:
            val = getattr(self.state, v, '<not set>')
            print(f"  {v}: {val}")
        print()

    def do_status(self, arg):
        """Show current configuration."""
        if getattr(self.state, 'silent', False):
            return
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
            
        folder_path = os.path.expanduser(arg)
        init_db(self.state.db)
        self._load_model()
        
        from processing import index_files
        is_silent = getattr(self.state, 'silent', False)
        index_files(folder_path, self.device, self.processor, self.model, self.state.db, 
                    batch_size=self.state.batch_size, max_num_patches=self.state.max_patches, 
                    fast_scene_detect=not self.state.accurate, silent=is_silent)

    def do_cleanup(self, arg):
        """Clean up orphaned database entries."""
        init_db(self.state.db)
        count = cleanup_orphaned_entries(self.state.db)
        if not getattr(self.state, 'silent', False):
            print(f'Removed {count} orphaned embeddings.')

    def do_exit(self, arg):
        """Exit the shell."""
        if not getattr(self.state, 'silent', False):
            print("Exiting...")
        return True
        
    def do_quit(self, arg):
        return self.do_exit(arg)


# --- Main CLI Entry ---
EXIT_SUCCESS = 0
EXIT_MODEL_ERROR = 1
EXIT_DB_ERROR = 2
EXIT_INVALID_INPUT = 3

def cli_mode():
    parser = argparse.ArgumentParser(description='Scene Scout CLI')
    parser.add_argument('--interactive', action='store_true', help='Enter interactive REPL mode')
    parser.add_argument('--json', action='store_true', help='Output search results in JSON format')
    parser.add_argument('--include-thumbs', action='store_true', help='Include base64 thumbnails in JSON output')
    parser.add_argument('--output', type=str, help='Write JSON output to file instead of stdout')
    parser.add_argument('--index', type=str, help='Path to folder to index')
    parser.add_argument('--search-text', type=str, help='Text to search for (use "-" for stdin)')
    parser.add_argument('--search-image', type=str, help='Image path to search with')
    parser.add_argument('--top-k', type=int, default=10, help='Results to return')
    parser.add_argument('--db', type=str, default='siglip2_embeddings.db', help='DB path')
    parser.add_argument('--device', type=str, choices=['cuda', 'cpu', 'dml', 'xpu', 'mps'], help='Force device')
    parser.add_argument('--max-patches', type=int, default=256, help='Max model patches')
    parser.add_argument('--batch-size', type=int, default=16, help='Inference batch size')
    parser.add_argument('--accurate', action='store_true', help='Use accurate detection')
    parser.add_argument('--cleanup', action='store_true', help='Clean orphaned entries')
    parser.add_argument('--silent', action='store_true', help='Suppress all non-essential output')
    args = parser.parse_args()

    # Handle stdin piping for search text
    if args.search_text == '-':
        try:
            args.search_text = sys.stdin.read().strip()
        except Exception as e:
            if not args.silent:
                print(f'Error reading from stdin: {e}', file=sys.stderr)
            sys.exit(EXIT_INVALID_INPUT)

    if args.interactive:
        SceneScoutShell(args).cmdloop()
        sys.exit(EXIT_SUCCESS)
        return

    # One-shot operations remain identical for backward compatibility
    try:
        init_db(args.db)
    except Exception as e:
        if not args.silent:
            print(f'Database error: {e}', file=sys.stderr)
        sys.exit(EXIT_DB_ERROR)

    if args.cleanup:
        try:
            count = cleanup_orphaned_entries(args.db)
            if not args.silent:
                print(f'Removed {count} orphaned embeddings.')
        except Exception as e:
            if not args.silent:
                print(f'Database cleanup error: {e}', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
        if not (args.index or args.search_text or args.search_image):
            sys.exit(EXIT_SUCCESS)

    if not (args.index or args.search_text or args.search_image):
        if not args.silent:
            print('No action specified. Use --help for options.')
        sys.exit(EXIT_INVALID_INPUT)

    try:
        from model_loader import load_siglip_model
        
        def status_callback(msg):
            if not args.silent:
                print(msg)
                
        model, processor, device, dtype, _ = load_siglip_model(args.device, status_callback=status_callback)
    except Exception as e:
        if not args.silent:
            print(f'Model error: {e}', file=sys.stderr)
        sys.exit(EXIT_MODEL_ERROR)

    if args.index:
        from processing import index_files
        try:
            folder_path = os.path.expanduser(args.index)
            index_files(folder_path, device, processor, model, args.db, 
                        batch_size=args.batch_size, max_num_patches=args.max_patches, 
                        fast_scene_detect=not args.accurate, silent=args.silent)
        except Exception as e:
            if not args.silent:
                print(f'Indexing error: {e}', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
    
    if args.search_text or args.search_image:
        try:
            run_search(args.search_text, args.search_image, device, processor, model, args)
        except Exception as e:
            if not args.silent:
                print(f'Search error: {e}', file=sys.stderr)
            sys.exit(EXIT_MODEL_ERROR)

    sys.exit(EXIT_SUCCESS)
