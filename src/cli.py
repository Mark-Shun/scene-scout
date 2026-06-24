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
import argparse
import os
import sys
import cmd
import shlex
import json
import base64
from pathlib import Path

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
    'qu': 'queue',
    'u': 'update',
    'ex': 'export',
    'rs': 'rescore',
    'v': 'verify',
    'rl': 'relink',
    'p': 'pack',
    'up': 'unpack'
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
        for path, scene_idx, start_time, end_time, thumb, score, source_db in results:
            entry = {
                "filepath": path,
                "filename": os.path.basename(path),
                "scene_index": scene_idx + 1 if scene_idx is not None else None,
                "start_time_ms": start_time,
                "end_time_ms": end_time,
                "score": round(score, 4),
                "source_database": source_db
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
    for i, (path, scene_idx, start_time, end_time, thumb, score, source_db) in enumerate(results, 1):
        time_str = format_time(start_time)
        if end_time is not None:
            time_str = f'{time_str}-{format_time(end_time)}'
        print(f'{i:2d}. [Scene {scene_idx+1} @ {time_str}] Score: {score:.4f} | {os.path.basename(path)} [{source_db}]')
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
    active_dbs = getattr(args, 'active_databases', [getattr(args, 'db', '')])
    if not active_dbs:
        if not getattr(args, 'silent', False):
            print('Error: No active databases for search.')
        return
    
    all_empty = all(db_is_empty(db) for db in active_dbs)
    if all_empty:
        if not getattr(args, 'silent', False):
            print('Warning: All databases appear to be empty. Please index files first.')
        return

    query_embedding = get_query_embedding(text, image, device, proc, model, args.max_patches)
    if query_embedding is not None:
        results = search_scenes(query_embedding, active_dbs, top_k=args.top_k)
        
        use_json = getattr(args, 'json', False)
        include_thumbs = getattr(args, 'include_thumbs', False)
        output_file = getattr(args, 'output', None)
        is_silent = getattr(args, 'silent', False)
        
        display_results(results, as_json=use_json, include_thumbs=include_thumbs, output_file=output_file, silent=is_silent)
    else:
        if not getattr(args, 'silent', False):
            print("Error: Could not generate query embedding.")


def run_cli_update(silent=False, auto_confirm=False):
    """Check for updates, prompt user, and trigger the handoff script."""
    from update_checker import check_for_update
    from update_manager import trigger_update_handoff, verify_environment
    import config

    if not silent:
        print("Checking for updates...")

    update_info = check_for_update()

    if not update_info or not update_info.get("update_available"):
        if not silent:
            print(f"You are up to date! (Current version: {update_info.get('current_version', 'Unknown')})")
        return

    print(f"\n[UPDATE AVAILABLE] Version {update_info['latest_version']} is available! (Current: {update_info.get('current_version', 'Unknown')})")

    notes = update_info.get('notes', 'No notes provided.')
    if len(notes) > 500:
        print(f"Release Notes:\n{notes[:500]}...\n")
    else:
        print(f"Release Notes:\n{notes}\n")

    if not auto_confirm:
        choice = input("Do you want to download and install this update now? (y/n): ").strip().lower()
        if choice != 'y':
            print("Update cancelled.")
            return

    try:
        target_dir = str(config.PROJECT_ROOT)
        if not verify_environment(target_dir):
            print("Error: Dependency pre-check failed. Network might be unstable.")
            return

        print("Downloading and preparing update...")

        from tqdm import tqdm
        pbar = tqdm(total=100, desc="Updating", unit="%")
        last_val = [0]

        def progress_callback(p):
            inc = p - last_val[0]
            if inc > 0:
                pbar.update(inc)
                last_val[0] = p

        # Determine if we should relaunch the CLI or exit cleanly (headless mode)
        target_mode = 'none' if silent else 'cli'

        trigger_update_handoff(
            download_url=update_info['download_url'],
            is_source_zip=update_info.get('is_source_zip', True),
            progress_callback=progress_callback,
            app_mode=target_mode
        )
        pbar.n = 100
        pbar.refresh()
        pbar.close()

        print("\n[SUCCESS] Update prepared successfully!")
        if target_mode == 'cli':
            print("The CLI will now restart to apply the files safely.")
        else:
            print("Update task complete. Exiting...")

        sys.exit(0)

    except Exception as e:
        print(f"\n[ERROR] Failed to apply update: {e}")


# --- Interactive Shell ---
class SceneScoutShell(cmd.Cmd):
    intro = "\nInteractive Mode Active. Type 'help' to list commands. Type 'exit' to quit."
    prompt = 'Scene Scout> '

    # Known variables for tab completion
    _settable_vars = ['json', 'include_thumbs', 'top_k', 'device', 'max_patches', 'batch_size', 'accurate', 'silent', 'output', 'generate_thumbnails']

    def __init__(self, initial_args, update_info=None):
        super().__init__()
        self.state = initial_args
        self.active_databases = list(getattr(initial_args, 'active_databases', []))
        self.target_db = getattr(initial_args, 'target_db', None)
        self.update_info = update_info
        self.active_databases = list(getattr(initial_args, 'active_databases', []))
        self.target_db = getattr(initial_args, 'target_db', None)
        self.model = None
        self.processor = None
        self.device = None
        self.last_results = None
        self.cached_embeddings = None
        self._load_history()

    def _get_effective_target(self):
        if self.target_db and os.path.exists(self.target_db):
            return self.target_db
        if self.active_databases:
            return self.active_databases[0]
        return None

    def _load_history(self):
        if not HISTORY_AVAILABLE or readline is None:
            return
        try:
            readline.read_history_file(HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(1000)

    def _db_status_callback(self, msg):
        if not getattr(self.state, 'silent', False):
            print(f"[INFO] {msg}")

    def postcmd(self, stop, line):
        if HISTORY_AVAILABLE and readline is not None:
            try:
                readline.write_history_file(HISTORY_FILE)
            except Exception:
                pass
        return stop

    def cmdloop(self, intro=None):
        try:
            while True:
                try:
                    line = self.cmdloop_line()
                    if line is None:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if parts[0] in COMMAND_ALIASES:
                        line = COMMAND_ALIASES[parts[0]]
                        if len(parts) > 1:
                            line += ' ' + parts[1]
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
        try:
            return input(self.prompt)
        except EOFError:
            print()
            return None

    def _load_model(self):
        if self.model is None:
            from model_loader import load_siglip_model
            
            use_json = getattr(self.state, 'json', False)
            is_silent = getattr(self.state, 'silent', False)
            def callback(msg):
                if not use_json and not is_silent:
                    print(msg)
                    
            self.model, self.processor, self.device, _, _ = load_siglip_model(
                self.state.device, status_callback=callback
            )

    def complete_set(self, text, line, begidx, endidx):
        return [v for v in self._settable_vars if v.startswith(text)]

    def do_db(self, arg):
        """Manage databases. Usage: db [ls | add <path> | rm <index> | target <index> | clear]"""
        args = shlex.split(arg)
        subcmd = args[0] if args else 'ls'
        
        if subcmd == 'ls':
            print("\n--- Active Databases ---")
            if not self.active_databases:
                print("  (none)")
            else:
                effective = self._get_effective_target()
                for i, db_path in enumerate(self.active_databases):
                    marker = " [TARGET]" if db_path == effective else ""
                    print(f"  [{i}] {os.path.basename(db_path)}{marker}")
                    print(f"      {db_path}")
            print()
            
        elif subcmd == 'add':
            if len(args) < 2:
                print("Usage: db add <path>")
                return
            path = os.path.expanduser(args[1])
            if not os.path.exists(path):
                print(f"Error: File not found: {path}")
                return
            abs_path = str(Path(path).resolve())
            if abs_path in self.active_databases:
                print("Database already in the list.")
                return
            self.active_databases.append(abs_path)
            try:
                init_db(abs_path, status_callback=self._db_status_callback)
            except Exception as e:
                print(f"Error initializing database: {e}")
                self.active_databases.remove(abs_path)
                return
            if not self.target_db:
                self.target_db = abs_path
            print(f"Added: {os.path.basename(abs_path)}")
            
        elif subcmd == 'rm':
            if len(args) < 2:
                print("Usage: db rm <index>")
                return
            try:
                idx = int(args[1])
            except ValueError:
                print("Invalid index. Please provide a numeric index.")
                return
            if idx < 0 or idx >= len(self.active_databases):
                print(f"Invalid index. Valid range: 0-{len(self.active_databases)-1}")
                return
            removed = self.active_databases.pop(idx)
            effective = self._get_effective_target()
            if not effective:
                self.target_db = None
            print(f"Removed: {os.path.basename(removed)}")
            
        elif subcmd == 'target':
            if len(args) < 2:
                print("Usage: db target <index>")
                return
            try:
                idx = int(args[1])
            except ValueError:
                print("Invalid index. Please provide a numeric index.")
                return
            if idx < 0 or idx >= len(self.active_databases):
                print(f"Invalid index. Valid range: 0-{len(self.active_databases)-1}")
                return
            self.target_db = self.active_databases[idx]
            print(f"Target set to: {os.path.basename(self.target_db)}")
            
        elif subcmd == 'clear':
            self.active_databases.clear()
            self.target_db = None
            print("All databases cleared.")
            
        else:
            print(f"Unknown db command: {subcmd}")
            print("Usage: db [ls | add <path> | rm <index> | target <index> | clear]")

    def do_load_db(self, arg):
        """Load an existing database file and add it to active databases."""
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
            abs_path = str(Path(db_path).resolve())
            init_db(abs_path, status_callback=self._db_status_callback)
            if abs_path not in self.active_databases:
                self.active_databases.append(abs_path)
            if not self.target_db:
                self.target_db = abs_path
            if not getattr(self.state, 'silent', False):
                print(f"Database loaded successfully: {abs_path}")
        except Exception as e:
            print(f"Failed to load database: {e}", file=sys.stderr)
            return 2

    def do_set(self, arg):
        """Set an editable variable. Usage: set <variable> <value>"""
        args = shlex.split(arg)
        if len(args) != 2:
            print("Usage: set <variable> <value>")
            return
        key, val = args[0], args[1]
        
        val_lower = val.lower()
        if val_lower in ['true', '1', 'yes', 'y']:
            parsed_val = True
        elif val_lower in ['false', '0', 'no', 'n']:
            parsed_val = False
        elif val.isdigit():
            parsed_val = int(val)
        else:
            parsed_val = val

        setattr(self.state, key, parsed_val)
        if not getattr(self.state, 'silent', False):
            print(f"{key} updated to {getattr(self.state, key)}")

    def do_vars(self, arg):
        """List all editable shell variables and their current values."""
        print("\n--- Editable Variables ---")
        for v in self._settable_vars:
            val = getattr(self.state, v, '<not set>')
            print(f"  {v}: {val}")
        effective = self._get_effective_target()
        print(f"  target_db: {effective}")
        print(f"  active_databases: {len(self.active_databases)}")
        print()

    def do_status(self, arg):
        """Show current shell state, active databases, and model load status."""
        if getattr(self.state, 'silent', False):
            return
        print("\n--- Current State ---")
        for k, v in vars(self.state).items():
            print(f"{k}: {v}")
        print(f"Active databases: {len(self.active_databases)}")
        print(f"Target database: {self._get_effective_target()}")
        print(f"Model Loaded: {self.model is not None}\n")

    def do_search(self, arg):
        """Search scenes using a text query or image path."""
        if not arg:
            print("Please provide a search query.")
            return
        
        if not self.active_databases:
            print("Error: No active databases. Use 'db add <path>' to add one.")
            return
        
        self._load_model()
        
        # Identify if query is an image path or text
        s_image = arg if (os.path.exists(arg) and arg.lower().endswith(config.IMAGE_EXTENSIONS)) else None
        s_text = None if s_image else arg
        
        saved_db = getattr(self.state, 'db', None)
        self.state.active_databases = self.active_databases
        
        from processing import get_query_embedding
        query_embedding = get_query_embedding(s_text, s_image, self.device, self.processor, self.model, self.state.max_patches)
        
        if query_embedding is None:
            print("Error: Could not generate query embedding.")
            self.state.db = saved_db
            return
        
        from database import search_scenes, db_is_empty
        all_empty = all(db_is_empty(db) for db in self.active_databases)
        if all_empty:
            print('Warning: All databases appear to be empty. Please index files first.')
            self.state.db = saved_db
            return
        
        # 1. Execute the primary search
        self.last_results = search_scenes(query_embedding, self.active_databases, top_k=self.state.top_k)
        self.state.db = saved_db
        
        if self.last_results:
            self.cached_embeddings = []
            from database import get_embedding_for_result
            
            # 2. Create a lookup map (Basename -> Absolute Path)
            db_path_map = {os.path.basename(p): p for p in self.active_databases}
            
            for res in self.last_results:
                path, scene_idx, start_time, end_time, thumb, score, source_db_name = res
                
                # 3. Resolve the full path before fetching the embedding
                full_db_path = db_path_map.get(source_db_name, source_db_name)
                emb = get_embedding_for_result(full_db_path, path, scene_idx)
                self.cached_embeddings.append(emb)
        
        # Display results to user
        use_json = getattr(self.state, 'json', False)
        include_thumbs = getattr(self.state, 'include_thumbs', False)
        output_file = getattr(self.state, 'output', None)
        is_silent = getattr(self.state, 'silent', False)
        
        display_results(self.last_results, as_json=use_json, include_thumbs=include_thumbs, output_file=output_file, silent=is_silent)

    def _parse_indices(self, index_str: str, max_val: int) -> list[int]:
        """Parses strings like '1,3,5-7' into a list of 0-based integers."""
        indices = set()
        for part in index_str.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                try:
                    start, end = map(int, part.split('-'))
                    indices.update(range(start, end + 1))
                except ValueError:
                    continue
            else:
                try:
                    indices.add(int(part))
                except ValueError:
                    continue
        return [i - 1 for i in indices if 0 < i <= max_val]

    def _run_headless_export(self, path: str, start_ms: int, end_ms: int, output_file: str, app_config: dict):
        from exporters.base_exporter import build_ffmpeg_args_headless, _get_cached_ffmpeg_path

        start_sec = start_ms / 1000.0
        duration_sec = (end_ms - start_ms) / 1000.0

        buffer_sec = 10.0
        fast_seek = max(0.0, start_sec - buffer_sec)
        exact_seek = start_sec - fast_seek

        cmd = [
            _get_cached_ffmpeg_path(),
            '-ss', str(fast_seek),
            '-i', path,
            '-ss', str(exact_seek),
        ]

        metadata = {'has_audio': True}
        cmd.extend(build_ffmpeg_args_headless(app_config, metadata))
        cmd.extend(['-map', '0:v:0', '-map', '0:a?'])
        cmd.extend(['-t', str(duration_sec), '-avoid_negative_ts', 'make_zero', '-y', output_file])

        creation_flags = 0
        if sys.platform == 'win32':
            creation_flags = subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags
        )
        _stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f'FFmpeg failed with code {process.returncode}: {stderr.decode()}')

    def do_export(self, arg):
        """Export scenes. Usage: export <1,2,5-8> <output_folder> OR export <1> <output_file.mp4>"""
        export_args = shlex.split(arg)
        if len(export_args) < 2:
            print("Usage: export <1,2,5-8> <output_folder> OR export <1> <output_file.mp4>")
            return

        if not self.last_results:
            print("No recent search results to export. Run a search first.")
            return

        indices = self._parse_indices(export_args[0], len(self.last_results))
        if not indices:
            print("No valid indices provided. Check your search results.")
            return

        target_path = export_args[1]
        is_bulk = len(indices) > 1 or os.path.isdir(target_path)

        app_config = config.load_config()

        for idx in indices:
            try:
                path, scene_idx, start_ms, end_ms, _, _, _ = self.last_results[idx]

                if is_bulk:
                    os.makedirs(target_path, exist_ok=True)
                    filename = f"{os.path.splitext(os.path.basename(path))[0]}_scene_{idx+1}.mp4"
                    output_file = os.path.join(target_path, filename)
                else:
                    output_file = target_path

                print(f"[{idx+1}/{len(indices)}] Exporting: {os.path.basename(path)}...")

                self._run_headless_export(path, start_ms, end_ms, output_file, app_config)

                if not is_bulk:
                    print("Export completed successfully.")

            except (ValueError, IndexError):
                print(f"  [ERROR] Scene {idx+1}: Invalid index.")
                continue
            except Exception as e:
                print(f"  [ERROR] Scene {idx+1} failed: {e}")
                continue

        if is_bulk and not getattr(self.state, 'silent', False):
            print(f"Bulk export finished. Processed {len(indices)} scene(s) to: {os.path.abspath(target_path)}")

    def do_rescore(self, arg):
        """Rescore the last search results with a new text query. Usage: rescore <query>"""
        if not arg:
            print("Please provide a text query for rescoring.")
            return
        if not self.last_results:
            print("No previous search results found to rescore.")
            return

        self._load_model()
        from processing import get_query_embedding
        import numpy as np

        rescore_emb = get_query_embedding(arg, None, self.device, self.processor, self.model, self.state.max_patches)
        if rescore_emb is None:
            print("Error: Could not generate rescore embedding.")
            return

        print(f"Rescoring with: '{arg}'...")

        rescored_results = []
        rescored_embeddings = []

        for i, res in enumerate(self.last_results):
            emb = self.cached_embeddings[i] if self.cached_embeddings and i < len(self.cached_embeddings) else None
            if emb is not None:
                new_score = float(np.dot(emb, rescore_emb.T).squeeze())
                updated_res = (res[0], res[1], res[2], res[3], res[4], new_score, res[6])
                rescored_results.append((updated_res, emb))

        rescored_results.sort(key=lambda x: x[0][5], reverse=True)

        self.last_results = [r[0] for r in rescored_results]
        self.cached_embeddings = [r[1] for r in rescored_results]

        use_json = getattr(self.state, 'json', False)
        is_silent = getattr(self.state, 'silent', False)
        display_results(self.last_results, as_json=use_json, silent=is_silent)

    def do_queue(self, arg):
            """View and manage the index queue. Usage: queue [ls | rm <id> | clear]"""
            args = shlex.split(arg)
            target = self._get_effective_target()
            if not target:
                print("Error: No target database set. Use 'db target <index>' to set one.")
                return
            init_db(target, status_callback=self._db_status_callback)
            from database import get_queue, remove_from_queue, clear_queue
            
            cmd_action = args[0] if args else 'ls'
            
            if cmd_action == 'ls':
                items = get_queue(target)
                if not items:
                    print("Queue is empty.")
                    return
                print("\n--- Current Queue ---")
                for qid, path, is_dir, rec in items:
                    item_type = "Folder" if is_dir else "File"
                    rec_text = " (Recursive)" if is_dir and rec else ""
                    print(f"[{qid}] {item_type}: {path}{rec_text}")
                print("---------------------")
                
            elif cmd_action == 'rm':
                if len(args) < 2:
                    print("Please specify an ID to remove. (e.g., queue rm 3)")
                    return
                try:
                    qid = int(args[1])
                    remove_from_queue(target, qid)
                    print(f"Removed item [{qid}] from the queue.")
                except ValueError:
                    print("Invalid ID. Please provide a numeric ID.")
                    
            elif cmd_action == 'clear':
                clear_queue(target)
                print("Queue cleared.")
                
            else:
                print("Unknown queue command. Use 'ls', 'rm <id>', or 'clear'.")

    def do_index(self, arg):
        """Add paths to the queue and process them for indexing."""
        if not arg:
            print("Please provide at least one path.")
            return
        
        target = self._get_effective_target()
        if not target:
            print("Error: No target database set. Use 'db target <index>' to set one.")
            return
            
        paths = shlex.split(arg)
        init_db(target, status_callback=self._db_status_callback)
        
        from database import add_to_queue
        added = 0
        for folder_path in paths:
            folder_path = os.path.expanduser(folder_path)
            is_dir = os.path.isdir(folder_path)
            if not is_dir and not folder_path.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS):
                print(f"Warning: Skipping invalid path: {folder_path}")
                continue
            add_to_queue(target, folder_path, is_directory=is_dir, recursive=is_dir)
            added += 1
        
        if added == 0:
            print("Error: No valid paths provided.")
            return
        
        self._load_model()
        
        from processing import index_files
        is_silent = getattr(self.state, 'silent', False)
        result = index_files(self.device, self.processor, self.model, target,
                    batch_size=self.state.batch_size, max_num_patches=self.state.max_patches,
                    fast_scene_detect=not self.state.accurate, silent=is_silent,
                    generate_thumbnails=getattr(self.state, 'generate_thumbnails', True))
        if result == 'completed':
            from database import clear_queue
            clear_queue(target)
            if not is_silent:
                print("Queue successfully processed and cleared.")

    def do_cleanup(self, arg):
        """Remove orphaned database entries for deleted files."""
        target = self._get_effective_target()
        if not target:
            print("Error: No target database set. Use 'db target <index>' to set one.")
            return
        init_db(target, status_callback=self._db_status_callback)
        count = cleanup_orphaned_entries(target)
        if not getattr(self.state, 'silent', False):
            print(f'Removed {count} orphaned embeddings.')

    def do_verify(self, arg):
        """Check the target database for missing or moved video files."""
        target = self._get_effective_target()
        if not target:
            print("Error: No target database set.")
            return

        from database import get_all_processed_videos
        videos = get_all_processed_videos(target)
        
        missing = [(vid, path) for vid, path in videos if not os.path.exists(path)]
        
        if not missing:
            print("All video files are present and accounted for.")
            return
            
        print(f"\nFound {len(missing)} missing video file(s):")
        for vid, path in missing:
            print(f"  [ID: {vid}] {path}")
        print("\nUse 'relink <ID> <new_path>' to fix a path, or 'cleanup' to permanently remove all missing entries.")

    def do_relink(self, arg):
        """Update the file path of a database entry. Usage: relink <ID> <new_path>"""
        args = shlex.split(arg)
        if len(args) < 2:
            print("Usage: relink <ID> <new_path>")
            return
        
        try:
            vid_id = int(args[0])
        except ValueError:
            print("Error: ID must be a number.")
            return
            
        new_path = os.path.expanduser(args[1])
        new_path = str(Path(new_path).resolve())
        
        if not os.path.exists(new_path):
            print(f"Error: The new file does not exist at: {new_path}")
            return
            
        target = self._get_effective_target()
        from database import update_video_filepath
        
        success = update_video_filepath(target, vid_id, new_path)
        if success:
            print(f"Successfully relinked entry {vid_id} to:\n  {new_path}")
        else:
            print(f"Error: The path '{new_path}' is already indexed in this database. You must use 'cleanup' to remove the orphaned entry instead.")
    
    def do_update(self, arg):
        """Check for and apply updates to Scene Scout.
        Usage: update"""
        run_cli_update(silent=False, auto_confirm=False)

    def do_pack(self, arg):
        """Pack active databases into a .scdb archive. Usage: pack <output_archive.scdb>"""
        if not arg:
            print("Usage: pack <output_archive.scdb>")
            return

        if not self.active_databases:
            print("Error: No active databases to pack. Use 'db add' first.")
            return

        out_path = os.path.expanduser(arg)
        if not out_path.endswith('.scdb'):
            out_path += '.scdb'

        print(f"Packing {len(self.active_databases)} database(s) into {out_path}...")

        import sqlite3
        import zipfile

        unique_videos = set()
        for db_path in self.active_databases:
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.execute("SELECT filepath FROM processed_videos WHERE status='completed'")
                    unique_videos.update(row[0] for row in cursor.fetchall() if os.path.exists(row[0]))
            except Exception as e:
                print(f"  [Warning] Failed to read {db_path}: {e}")

        try:
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_STORED) as archive:
                for db_path in self.active_databases:
                    print(f"  -> Adding Database: {os.path.basename(db_path)}")
                    archive.write(db_path, os.path.basename(db_path))

                for path in unique_videos:
                    print(f"  -> Archiving Video: {os.path.basename(path)}")
                    archive.write(path, f"videos/{os.path.basename(path)}")

            print("Pack complete!")
        except Exception as e:
            print(f"Error packing archive: {e}")

    def do_unpack(self, arg):
        """Unpack a .scdb archive. Usage: unpack <archive.scdb> <destination_folder>"""
        args = shlex.split(arg)
        if len(args) < 2:
            print("Usage: unpack <archive.scdb> <destination_folder>")
            return

        archive_path = os.path.expanduser(args[0])
        target_dir = os.path.expanduser(args[1])

        if not os.path.exists(archive_path):
            print(f"Error: Archive not found at {archive_path}")
            return

        import zipfile
        from database import remap_all_video_paths

        print(f"Unpacking {os.path.basename(archive_path)} to {target_dir}...")
        os.makedirs(target_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(archive_path, 'r') as archive:
                archive.extractall(target_dir)

            extracted_dbs = [f for f in archive.namelist() if f.endswith('.db') and '/' not in f]
            videos_folder = os.path.join(target_dir, "videos")

            for db_name in extracted_dbs:
                db_path = os.path.join(target_dir, db_name)
                print(f"  -> Recalibrating paths for {db_name}...")
                remap_all_video_paths(db_path, videos_folder)

                if db_path not in self.active_databases:
                    self.active_databases.append(db_path)
                    if not self.target_db:
                        self.target_db = db_path

            print(f"Unpack successful. {len(extracted_dbs)} database(s) added to active workspace.")
        except Exception as e:
            print(f"Error unpacking archive: {e}")

    def do_exit(self, arg):
        """Exit the interactive shell."""
        if not getattr(self.state, 'silent', False):
            print("Exiting...")
        return True
        
    def do_quit(self, arg):
        """Exit the interactive shell (alias for exit)."""
        return self.do_exit(arg)


# --- Main CLI Entry ---
EXIT_SUCCESS = 0
EXIT_MODEL_ERROR = 1
EXIT_DB_ERROR = 2
EXIT_INVALID_INPUT = 3

def cli_mode(update_info=None):
    parser = argparse.ArgumentParser(description='Scene Scout CLI')
    parser.add_argument('--interactive', action='store_true', help='Enter interactive REPL mode')
    parser.add_argument('--json', action='store_true', help='Output search results in JSON format')
    parser.add_argument('--include-thumbs', action='store_true', help='Include base64 thumbnails in JSON output')
    parser.add_argument('--output', type=str, help='Write JSON output to file instead of stdout')
    parser.add_argument('--show-queue', action='store_true', help='Show the current index queue')
    parser.add_argument('--remove-queue', type=int, action='append', help='Remove an item from the queue by ID (can be used multiple times)')
    parser.add_argument('--clear-queue', action='store_true', help='Clear the entire index queue')
    parser.add_argument('--index', type=str, action='append', help='Path to folder or file to index (can be specified multiple times)')
    parser.add_argument('--search-text', type=str, help='Text to search for (use "-" for stdin)')
    parser.add_argument('--search-image', type=str, help='Image path to search with')
    parser.add_argument('--top-k', type=int, default=10, help='Results to return')
    parser.add_argument('--db', type=str, action='append', dest='db', default=None, help='Database path(s) for search (can be specified multiple times)')
    parser.add_argument('--target-db', type=str, default=None, help='Database path for indexing/queue operations')
    parser.add_argument('--device', type=str, choices=['cuda', 'cpu', 'dml', 'xpu', 'mps'], help='Force device')
    parser.add_argument('--max-patches', type=int, default=256, help='Max model patches')
    parser.add_argument('--batch-size', type=int, default=16, help='Inference batch size')
    parser.add_argument('--accurate', action='store_true', help='Use accurate detection')
    parser.add_argument('--cleanup', action='store_true', help='Clean orphaned entries')
    parser.add_argument('--silent', action='store_true', help='Suppress all non-essential output')
    parser.add_argument('--export-scene', type=str, help='Path of the video to export a scene from')
    parser.add_argument('--start', type=int, help='Start time of the scene in milliseconds')
    parser.add_argument('--end', type=int, help='End time of the scene in milliseconds')
    parser.add_argument('--out', type=str, help='Output file path for the exported video')
    parser.add_argument('--crf', type=int, default=None, help='Quality (0-51, lower=better, default=23)')
    parser.add_argument('--video-codec', type=str, default=None, choices=['H.264 (libx264)', 'H.265 (libx265)', 'AV1 (libsvtav1)', 'VP9 (libvpx-vp9)', 'ProRes 422 (prores_ks)'], help='Video codec for export')
    parser.add_argument('--audio-mode', type=str, default=None, choices=['copy', 'encode', 'disable'], help='Audio mode for export')
    parser.add_argument('--audio-codec', type=str, default=None, help='Audio codec for export (e.g. AAC (aac), MP3 (libmp3lame))')
    parser.add_argument('--audio-bitrate', type=str, default=None, help='Audio bitrate (e.g. 128k, 192k, 256k, 320k)')
    parser.add_argument('--resolution', type=str, default=None, help='Output resolution (e.g. 1080p, 720p, 480p, or Custom 1920x1080)')
    parser.add_argument('--pack', type=str, help='Pack active databases into the specified .scdb archive path')
    parser.add_argument('--unpack', nargs=2, metavar=('ARCHIVE', 'DEST'), help='Unpack a .scdb archive to a destination folder')
    parser.add_argument('--verify', action='store_true', help='Verify all video paths in the target database')
    parser.add_argument('--relink', nargs=2, metavar=('ID', 'NEW_PATH'), help='Relink a broken video path in the database')
    parser.add_argument('--update', action='store_true', help='Check for and apply updates')
    parser.add_argument('--yes', action='store_true', help='Automatically confirm prompts (useful for unattended updates)')
    args = parser.parse_args()

    if args.update:
        run_cli_update(silent=args.silent, auto_confirm=args.yes)
        sys.exit(0)

    def cli_migration_callback(msg):
        if not args.silent:
            print(f"[INFO] {msg}")

    saved_config = config.load_config()
    
    if args.db:
        active_dbs = [str(Path(p).resolve()) for p in args.db]
    else:
        saved_dbs = saved_config.get('active_databases', [])
        active_dbs = [p for p in saved_dbs if os.path.exists(p)] if saved_dbs else ['siglip2_embeddings.db']
    
    if args.target_db:
        target_db = str(Path(args.target_db).resolve())
    else:
        saved_primary = saved_config.get('primary_database', '')
        if saved_primary and os.path.exists(saved_primary):
            target_db = saved_primary
        elif active_dbs:
            target_db = active_dbs[0]
        else:
            target_db = None
    
    args.active_databases = active_dbs
    args.target_db = target_db

    if args.search_text == '-':
        try:
            args.search_text = sys.stdin.read().strip()
        except Exception as e:
            if not args.silent:
                print(f'Error reading from stdin: {e}', file=sys.stderr)
            sys.exit(EXIT_INVALID_INPUT)

    if args.interactive:
        SceneScoutShell(args, update_info).cmdloop()
        sys.exit(EXIT_SUCCESS)
        return

    if args.export_scene:
        if args.start is None or args.out is None:
            if not args.silent:
                print('Error: --start and --out are required for exporting scenes.', file=sys.stderr)
            sys.exit(EXIT_INVALID_INPUT)

        try:
            from exporters.base_exporter import build_ffmpeg_args_headless, _get_cached_ffmpeg_path

            temp_config = saved_config.copy()
            if args.crf is not None:
                temp_config['export_crf'] = args.crf
            if args.video_codec is not None:
                temp_config['export_video_codec'] = args.video_codec
            if args.audio_mode is not None:
                temp_config['export_audio_mode'] = args.audio_mode
            if args.audio_codec is not None:
                temp_config['export_audio_codec'] = args.audio_codec
            if args.audio_bitrate is not None:
                temp_config['export_audio_bitrate'] = args.audio_bitrate
            if args.resolution is not None:
                temp_config['export_resolution'] = args.resolution

            import subprocess
            start_sec = args.start / 1000.0
            duration_sec = ((args.end or args.start + 1000) - args.start) / 1000.0

            buffer_sec = 10.0
            fast_seek = max(0.0, start_sec - buffer_sec)
            exact_seek = start_sec - fast_seek

            cmd = [
                _get_cached_ffmpeg_path(),
                '-ss', str(fast_seek),
                '-i', args.export_scene,
                '-ss', str(exact_seek),
            ]

            metadata = {'has_audio': True}
            cmd.extend(build_ffmpeg_args_headless(temp_config, metadata))
            cmd.extend(['-map', '0:v:0', '-map', '0:a?'])
            cmd.extend(['-t', str(duration_sec), '-avoid_negative_ts', 'make_zero', '-y', args.out])

            if not args.silent:
                print(f'Exporting scene to {args.out}...')

            creation_flags = 0
            if sys.platform == 'win32':
                creation_flags = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags
            )
            _stdout, stderr = process.communicate()

            if process.returncode != 0:
                raise RuntimeError(f'FFmpeg failed with code {process.returncode}: {stderr.decode()}')

            if not args.silent:
                print('Export completed successfully.')
            sys.exit(EXIT_SUCCESS)

        except ImportError:
            if not args.silent:
                print('Error: Exporter module not found.', file=sys.stderr)
            sys.exit(EXIT_MODEL_ERROR)
        except Exception as e:
            if not args.silent:
                print(f'Export failed: {e}', file=sys.stderr)
            sys.exit(EXIT_MODEL_ERROR)

    for db_path in active_dbs:
        try:
            init_db(db_path, cli_migration_callback)
        except Exception as e:
            if not args.silent:
                print(f'Database error ({db_path}): {e}', file=sys.stderr)

    if target_db:
        try:
            init_db(target_db, cli_migration_callback)
        except Exception as e:
            if not args.silent:
                print(f'Target database error: {e}', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)

    if args.cleanup:
        db_to_cleanup = target_db if target_db else active_dbs[0] if active_dbs else None
        if not db_to_cleanup:
            if not args.silent:
                print('Error: No database specified for cleanup.', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
        try:
            count = cleanup_orphaned_entries(db_to_cleanup)
            if not args.silent:
                print(f'Removed {count} orphaned embeddings.')
        except Exception as e:
            if not args.silent:
                print(f'Database cleanup error: {e}', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
        if not (args.index or args.search_text or args.search_image or args.show_queue or args.remove_queue or args.clear_queue):
            sys.exit(EXIT_SUCCESS)

    if not (args.index or args.search_text or args.search_image or args.show_queue or args.remove_queue or args.clear_queue or args.verify or args.relink or args.pack or args.unpack):
        if not args.silent:
            print('No action specified. Use --help for options.')
        sys.exit(EXIT_INVALID_INPUT)

    if args.verify:
        if not target_db:
            print('Error: No target database specified.', file=sys.stderr)
        else:
            from database import get_all_processed_videos
            videos = get_all_processed_videos(target_db)
            missing = [(vid, path) for vid, path in videos if not os.path.exists(path)]
            if not args.silent:
                if missing:
                    print(f"Found {len(missing)} missing video(s):")
                    for vid, p in missing:
                        print(f"  [ID: {vid}] {p}")
                else:
                    print("All videos verified successfully.")

    if args.relink:
        if not target_db:
            print('Error: No target database specified.', file=sys.stderr)
        else:
            from database import update_video_filepath
            vid_id, new_path = int(args.relink[0]), os.path.abspath(args.relink[1])
            if update_video_filepath(target_db, vid_id, new_path):
                if not args.silent:
                    print(f"Successfully relinked ID {vid_id} to {new_path}")
            else:
                print(f"Error: Failed to relink ID {vid_id}.", file=sys.stderr)

    if args.pack:
        if not active_dbs:
            print('Error: No active databases to pack.', file=sys.stderr)
        else:
            temp_shell = SceneScoutShell(args, update_info)
            temp_shell.active_databases = active_dbs
            temp_shell.do_pack(args.pack)

    if args.unpack:
        archive_path, dest_dir = args.unpack
        temp_shell = SceneScoutShell(args, update_info)
        temp_shell.do_unpack(f'"{archive_path}" "{dest_dir}"')

    if not (args.index or args.search_text or args.search_image or args.show_queue or args.remove_queue or args.clear_queue):
        sys.exit(EXIT_SUCCESS)

    if args.show_queue or args.remove_queue or args.clear_queue or args.index:
        if not target_db:
            if not args.silent:
                print('Error: No target database set for queue/index operations. Use --target-db.', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)

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
        
    if args.clear_queue:
        from database import clear_queue
        clear_queue(target_db)
        if not args.silent:
            print("Queue cleared.")
            
    if args.remove_queue:
        from database import remove_from_queue
        for qid in args.remove_queue:
            remove_from_queue(target_db, qid)
            if not args.silent:
                print(f"Removed ID [{qid}] from queue.")

    if args.show_queue:
        from database import get_queue
        items = get_queue(target_db)
        if not args.silent:
            if not items:
                print("Queue is empty.")
            else:
                print("\n--- Current Queue ---")
                for qid, path, is_dir, rec in items:
                    item_type = "Folder" if is_dir else "File"
                    rec_text = " (Recursive)" if is_dir and rec else ""
                    print(f"[{qid}] {item_type}: {path}{rec_text}")
                print("---------------------")

    if args.index:
        from processing import index_files
        from database import add_to_queue
        try:
            added = 0
            for path in args.index:
                expanded = os.path.expanduser(path)
                is_dir = os.path.isdir(expanded)
                if not is_dir and not expanded.lower().endswith(config.IMAGE_EXTENSIONS + config.VIDEO_EXTENSIONS):
                    if not args.silent:
                        print(f'Warning: Skipping invalid path: {path}', file=sys.stderr)
                    continue
                add_to_queue(target_db, expanded, is_directory=is_dir, recursive=is_dir)
                added += 1
            if added == 0:
                if not args.silent:
                    print('Error: No valid paths provided.', file=sys.stderr)
                sys.exit(EXIT_INVALID_INPUT)
            index_files(device, processor, model, target_db,
                        batch_size=args.batch_size, max_num_patches=args.max_patches,
                        fast_scene_detect=not args.accurate, silent=args.silent,
                        generate_thumbnails=True)
        except Exception as e:
            if not args.silent:
                print(f'Indexing error: {e}', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
    
    if args.search_text or args.search_image:
        if not active_dbs:
            if not args.silent:
                print('Error: No active databases for search. Use --db to specify.', file=sys.stderr)
            sys.exit(EXIT_DB_ERROR)
        try:
            run_search(args.search_text, args.search_image, device, processor, model, args)
        except Exception as e:
            if not args.silent:
                print(f'Search error: {e}', file=sys.stderr)
            sys.exit(EXIT_MODEL_ERROR)

    sys.exit(EXIT_SUCCESS)
