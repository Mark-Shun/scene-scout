# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-05-05

A pretty big update, focused on bringing more functionality to the tool and enhancing existing features.

### Main highlights
- **File handling and queue system**: It is now possible to parse multiple files, folders and index them all at once. There is also a GUI list component in which details about these files can be found and options changed.
- **Database manager system**: Through the database manager system, it is possible to import multiple database files and dynamically search through them. Besides that there is info shown about the database and it's possible to merge database files into a new file.
- **Enhanced update checker**: More data is now being retrieved from Github when there is a new release. And the update details are now shown on the GUI.
- **Scene Export**: Export specific video scenes with customizable FFmpeg settings. Supports Stream Copy (fast) and Re-encode (exact frame accuracy) modes with progress tracking.
- **Enhanced update checker**: More data is now being retrieved from Github when there is a new release. And the update details are now shown on the GUI.

### Added
- **Scene Export**: Export video scenes with FFmpeg integration via a dedicated dialog
- **Media Queue System**: Replace single-folder indexing with a queue-based system supporting multiple files and folders
- **Queue Manager Popup**: Inspect, modify, and manage queued items with a Treeview list view
- **Drag & Drop to Queue**: Drop files/folders directly onto the queue area in the GUI
- **Per-folder Recursive Toggle**: Control whether each folder scans subdirectories independently (via Queue Manager)
- **Missing Path Detection**: Queue Manager shows `[MISSING]` tags and a "Clean Missing" button for deleted paths
- **CLI Multi-path Indexing**: `--index` now accepts multiple paths (e.g., `--index /path1 --index /path2`)
- **REPL Multi-path Indexing**: Interactive shell `index` command now supports multiple space-separated paths
- **Queue Persistence**: Index queue is stored per-database in SQLite and persists across sessions
- **Automatic Migration**: Old `folder_path` config values are automatically migrated to the queue on first load
- **Rich Update Notifications**: GUI now displays a formatted popup window with release notes when a new version is detected.
- **Markdown Release Notes**: Integrated a custom parser to render GitHub release notes with headers, bold text, and bullet points.
- **CLI Update Command**: Added a dedicated `update` (alias `u`) command to the interactive shell to view full patch notes on demand.
- **Rich Update Notifications**: GUI now displays a formatted popup window with release notes when a new version is detected.
- **Markdown Release Notes**: Integrated a custom parser to render GitHub release notes with headers, bold text, and bullet points.
- **CLI Update Command**: Added a dedicated `update` (alias `u`) command to the interactive shell to view full patch notes on demand.
- **Multi-Database Search**: Query multiple databases at once with results merged, sorted, and deduplicated by file path and scene index
- **Database Manager Popup**: Full management interface showing database name, path, scene/video/image counts, and total stats
- **Search Source Column**: Results list and CLI output now display which database each result originated from
- **CLI Multi-Database Support**: `--db` flag now accepts multiple databases (`--db a.db --db b.db`), with new `--target-db` flag for indexing operations
- **Interactive Shell `db` Commands**: `db ls`, `db add`, `db rm`, `db target`, `db clear` for managing databases in REPL mode
- **Database Statistics**: `get_db_stats()` function returns scene count, video count, image count, and file size per database
- **Search results sorting**: Simple in memory sorting of the search results from the database. Sorting them according to the available columns.
- **Standalone Export Function**: `export_video_scene()` in exporter.py for headless FFmpeg-based scene extraction

### Changed
- **Database Schema v2**: Added `index_queue` table for tracking files/directories to process
- **Processing Logic**: Refactored `index_files()` to use queue-based file flattening with deduplication
- **GUI Layout**: Replaced "Folder to process" section with "Media Queue" section
- **Config**: Removed `folder_path` from default configuration (queue is now stored in SQLite)
- **Automatic Payload Cleaning**: Release notes are now pre-processed to strip raw HTML image tags and markdown graphics for a cleaner interface.
- **Improved GUI Modality**: Isolated scrolling behavior so the main application background remains stationary while the update dialog is active.

### Fixed
- **Markdown Link Parsing**: URLs are now hidden in the GUI, displaying only the relevant text labels for better readability.
- **Automatic Payload Cleaning**: Release notes are now pre-processed to strip raw HTML image tags and markdown graphics for a cleaner interface.
- **Improved GUI Modality**: Isolated scrolling behavior so the main application background remains stationary while the update dialog is active.
- **Config Structure**: Replaced `db_path` with `active_databases` (list) and `primary_database` (string) — old configs are automatically migrated
- **Database Section UI**: Replaced listbox with compact target label, search count indicator, and prominent "Manage Databases..." button
- **Search Function Signatures**: `search_scenes()` and `search_db()` now accept a list of database paths instead of a single path
- **CLI Search Logic**: `run_search()` now queries all active databases and merges results
- **Interactive Shell State Tracking**: Shell now maintains its own `active_databases` list and `target_db` pointer, independent of argparse defaults

### Installation note
Due to the usage of a new package (imageio-ffmpeg for ffmpeg functions), you might have to run the install script again.


## [1.0.1] - 2026-5-04

### Fixed
- **CLI** - CLI mode was not using the new database format with newly added thumbnails

### Added
- **Silent Mode (`--silent`)**: Suppresses all non-essential output including progress bars, making CLI output clean for automation and scripting
- **Byte64 thumbnail**: Added ability for to return thumbnail data in Byte64 format
- **JSON File Output (`--output FILE`)**: Write JSON results directly to a file instead of stdout, preventing log/output mixing
- **Stdin Piping**: Use `--search-text -` to pipe queries from stdin (e.g., `echo "query" | python scenescout.py --search-text - --json`)
- **Interactive Shell Aliases**: Shortcut commands (`s` for search, `i` for index, `cl` for cleanup, `ls` for status, `h` for help, `q` for exit)
- **Tab Completion**: Press Tab in interactive mode to autocomplete variable names when using `set`
- **Command History**: Persistent Up Arrow command history across sessions (stored in `~/.scene_scout_history`)
- **Variables Command (`vars`)**: Lists all editable interactive shell variables with their current values
- **Standardized Exit Codes**: Return codes for pipeline integration (`0` success, `1` model error, `2` database error, `3` invalid input)
- **Path Expansion**: Tilde (`~`) and home directory paths are automatically expanded in index and load_db commands
- **Added tooltips**: Explenations for different buttons and fields in the GUI when hovering over them

### Changed
- **Silent Mode in Processing**: All tqdm progress bars and PySceneDetect output are now suppressible via `--silent` flag

## Post-fork [1.0.0] - 2026-5-04

### Added
- **Wide GPU processing support**: Original project had limited support. Now at this moment: CPU, CUDA, TensorRT, DirectML, Intel XPU, AMD Rocm and Apple MPS
- **Video playback**: Added support for video scene playback through VLC backend.
- **Thumbnail**: Shows low quality small thumbnails of search results.
- **Scene time extraction**: Added support to extract time data of scenes (scene change detect and extraction from metadata).
- **Scene indexing**: Index and search for scenes inside the app.
- **UV installation**: New installation method by using UV for new users.
- **Themes**: GUI now supports different TTK themes
- **Automated installation**: Various scripts to automate installation
- **Interactive shell**: An interactive CLI to interact with scene scout in the terminal while keeping the weights loaded.
- **Options**: Added various options which are saved in a config file
- **Drag and drop**: Drag and drop database files and folders to automatically load into scene scout.
- **Update checking**: Though not automatic added an update check with indication where to download it.

### Changed
- **Refactor file functions**: Refactored the code to seperate functionality into different files.
- **Focus**: Original project was focused on finding files (pictures/videos as whole), this project focuses on finding scenes.
- **Loading lock**: While loading or downloading the models, lock/hide the main GUI.

## Pre-fork [2.0.0] - 2025-11-28

### Added

- **Configuration Persistence**: Settings (database path, folder path) are now saved to `siglip2_config.json` and persist between sessions.
- **Image Preview Controls**: Added Pan (drag) and Zoom (mouse wheel) functionality to the image preview canvas.
- **Rescoring**: New "Rescore" feature allows refining search results with a secondary text query.
- **Batch Processing**: Image indexing is now batched (default size: 10) for improved performance.
- **GUI Improvements**:
  - Dedicated "Load Model" button.
  - Clear separation between "Index" and "Search" workflows.
  - Improved status feedback.
- **Recursive Indexing**: Now uses `rglob` to find images and videos in all subdirectories.

### Changed

- **Script Name**: Renamed main script from `sigLIP2_en.py` to `S2N_Search.py`.
- **Database Schema**: Updated schema for better compatibility.
- **Video Indexing**: Simplified video frame extraction to 'uniform' method for consistency.

### Removed

- **Legacy Video Methods**: Removed 'start', 'mid', 'end' extraction methods in favor of 'uniform'.
- **Threshold Slider**: Removed UI slider; thresholding is now handled internally or via code if needed.