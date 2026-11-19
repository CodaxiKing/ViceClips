"""Transcrição local da fala (faster-whisper), com o tempo de cada palavra.

Roda na CPU do próprio servidor, sem API. O modelo é baixado uma vez para
WORK_DIR/models. Sem o pacote faster-whisper instalado, tudo aqui devolve vazio
e os clips simplesmente saem sem legenda.
"""
import os
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from .. import config

MODEL_SIZE = os.environ.get("VICECLIPS_WHISPER_MODEL", "small")
MIN_PROBABILITY = 0.35     # abaixo disso a palavra costuma ser invenção sobre ruído/música
_model = None
_lock = threading.Lock()


@dataclass
class Word:
    start: float
    end: float
    text: str


def available() -> bool:
    if os.environ.get("VICECLIPS_CAPTIONS", "1") == "0":
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_SIZE, compute_type="int8",
                              download_root=str(config.WORK_DIR / "models"))
    return _model


def _audio(path: str) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def transcribe_words(path: str) -> list[Word]:
    """Palavras faladas no clip, em ordem, com início e fim em segundos."""
    if not available():
        return []
    audio = _audio(path)
    if len(audio) < 16000:
        return []
    with _lock:   # um clip por vez: o modelo já usa todos os núcleos
        segments, _ = _load().transcribe(audio, word_timestamps=True, vad_filter=True,
                                         condition_on_previous_text=False)
        words = []
        for segment in segments:
            if segment.no_speech_prob > 0.6:
                continue
            for w in segment.words or []:
                text = w.word.strip()
                if text and w.probability >= MIN_PROBABILITY:
                    words.append(Word(float(w.start), float(w.end), text))
    return words


def transcribe(path: str) -> str | None:
    """Texto corrido (usado para sugerir o título)."""
    return " ".join(w.text for w in transcribe_words(path)) or None
