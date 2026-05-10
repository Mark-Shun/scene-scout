import torch

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
