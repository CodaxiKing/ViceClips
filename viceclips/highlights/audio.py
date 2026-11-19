"""Candidatos a clip por pico de áudio (gritos, risadas, reação do streamer).

Primeiro sinal, barato e sem modelo. Picos de chat e transcrição entram depois
como sinais adicionais na mesma interface: lista de (início, fim, score).
"""
import subprocess

import numpy as np

RATE = 8000


def loudness_per_second(path: str) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"], capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, np.int16).astype(np.float32)
    secs = len(pcm) // RATE
    if secs == 0:
        return np.zeros(0, np.float32)
    return np.sqrt((pcm[: secs * RATE].reshape(secs, RATE) ** 2).mean(axis=1))


def find_highlights(path: str, count: int = 5, pre: float = 20, post: float = 10,
                    ) -> list[tuple[float, float, float]]:
    """Top `count` janelas (início, fim, score), sem sobreposição, em ordem cronológica."""
    rms = loudness_per_second(path)
    if len(rms) < 3:
        return []
    smooth = np.convolve(rms, np.ones(3) / 3, mode="same")
    score = (smooth - np.median(smooth)) / (smooth.std() + 1e-6)
    picked: list[tuple[float, float, float]] = []
    for t in np.argsort(score)[::-1]:
        if len(picked) == count or score[t] <= 1.0:
            break
        start, end = max(0.0, float(t) - pre), min(float(len(rms)), float(t) + post)
        if all(end <= s or start >= e for s, e, _ in picked):
            picked.append((start, end, float(score[t])))
    return sorted(picked)
