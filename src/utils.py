import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

import torch

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


def normalize_embedding(features: torch.Tensor) -> torch.Tensor:
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
