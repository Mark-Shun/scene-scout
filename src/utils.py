import torch

def normalize_embedding(features: torch.Tensor) -> torch.Tensor:
    return features / torch.linalg.norm(features, ord=2, dim=-1, keepdim=True)
