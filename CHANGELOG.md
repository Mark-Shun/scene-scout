# Changelog

All notable changes to this project will be documented in this file.

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