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
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import config


def setup_logging():
    """Initializes a globally accessible, thread-safe rotating logging system."""

    app_config = config.load_config()
    log_level_str = app_config.get("log_level", "INFO").upper()

    levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    log_level = levels.get(log_level_str, logging.INFO)

    logger = logging.getLogger()
    logger.setLevel(log_level)

    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )

    console_handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(threadName)-15s | %(filename)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error("Uncaught exception in main thread", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception

    def handle_thread_exception(args):
        logger.error(
            f"Uncaught exception in thread: {args.thread.name if args.thread else 'Unknown'}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
        )

    threading.excepthook = handle_thread_exception

    logging.info("=" * 50)
    logging.info("Scene Scout Application Started")
    logging.info("=" * 50)


def check_environment_packages():
    import importlib.util
    missing = []
    for pkg in config.CRITICAL_DEPENDENCIES:
        # Use importlib.util for a cleaner, non-intrusive check
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)
            
    if missing:
        # Construct a detailed error message
        if sys.platform == 'win32':
            install_script="windows-install.bat"
        elif sys.platform == 'linux':
            install_script="linux-install.sh"
        elif sys.platform == 'darwin':
            install_script="mac-install.sh"
        msg = (
            "Scene Scout encountered a critical environment error.\n\n"
            f"Missing packages: {', '.join(missing)}\n\n"
            "This usually happens if the environment was moved or updated manually.\n"
            f"Repair by running the {install_script} again."
        )
        # Log to file before printing to console, ensuring developers have the record
        logging.critical(msg)
        sys.exit(1)


def normalize_embedding(features):
    import torch
    return features / torch.linalg.norm(features, ord=2, dim=-1, keepdim=True)


def _get_ffmpeg_path() -> str:
    global _FFMPEG_PATH_CACHE
    if _FFMPEG_PATH_CACHE:
        return _FFMPEG_PATH_CACHE

    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        import shutil
        path = shutil.which('ffmpeg') or 'ffmpeg'

    _FFMPEG_PATH_CACHE = path
    return path
