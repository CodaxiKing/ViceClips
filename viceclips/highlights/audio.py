"""Melhores momentos de uma live gravada, a partir do áudio inteiro (ver analysis.py)."""
from .analysis import WARMUP, Moment, audio_features, pick_moments

INTRO_SECONDS = 300       # tela de "já vai começar": música alta, nada para clipar
LONG_STREAM = 1800


def find_highlights(path: str, count: int = 5) -> list[Moment]:
    """Até `count` momentos (início, fim, nota, tipo), em ordem cronológica."""
    feats = audio_features(path)
    # Em live longa, os primeiros minutos são a abertura; em vídeo curto, vale tudo.
    skip = INTRO_SECONDS if len(feats) > LONG_STREAM else WARMUP
    return pick_moments(feats, count, warmup=skip)
