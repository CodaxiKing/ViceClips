"""Melhores momentos de uma live gravada, a partir do áudio inteiro (ver analysis.py)."""
from .analysis import Moment, audio_features, pick_moments


def find_highlights(path: str, count: int = 5) -> list[Moment]:
    """Até `count` momentos (início, fim, nota, tipo), em ordem cronológica."""
    return pick_moments(audio_features(path), count)
