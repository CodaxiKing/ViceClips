"""Transcrição local da fala (faster-whisper), com o tempo de cada palavra.

Roda na CPU do próprio servidor, sem API. O modelo é baixado uma vez para
WORK_DIR/models. Sem o pacote faster-whisper instalado, tudo aqui devolve vazio
e os clips simplesmente saem sem legenda.

Só a fala do streamer vira legenda: o áudio é dividido em blocos de fala e fica
apenas o que estiver no idioma dele (VICECLIPS_CAPTION_LANG, padrão português).
O diálogo do jogo, em inglês, é descartado.
"""
import os
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

from .. import config

MODEL_SIZE = os.environ.get("VICECLIPS_WHISPER_MODEL", "small")
# Idioma do streamer; vazio legenda toda fala que aparecer, inclusive a do jogo.
CAPTION_LANG = os.environ.get("VICECLIPS_CAPTION_LANG", "pt").strip().lower()
MIN_PROBABILITY = 0.35     # abaixo disso a palavra costuma ser invenção sobre ruído/música
MIN_LANG_PROBABILITY = 0.5  # certeza mínima de que o bloco está no idioma do streamer
RATE = 16000
BLOCK_GAP = 1.0            # pausa (s) que separa dois blocos de fala
BLOCK_MAX = 15.0           # blocos longos são divididos, para o idioma ser conferido por trecho
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
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def speech_blocks(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Junta trechos de fala próximos em blocos de até BLOCK_MAX segundos."""
    blocks: list[list[float]] = []
    for start, end in spans:
        if blocks and start - blocks[-1][1] <= BLOCK_GAP and end - blocks[-1][0] <= BLOCK_MAX:
            blocks[-1][1] = end
        else:
            blocks.append([start, end])
    return [(a, b) for a, b in blocks]


def _speech_spans(audio: np.ndarray) -> list[tuple[float, float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    stamps = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300))
    return [(s["start"] / RATE, s["end"] / RATE) for s in stamps]


def _in_language(model, chunk: np.ndarray, lang: str) -> bool:
    _, _, ranked = model.detect_language(chunk)
    return dict(ranked).get(lang, 0.0) >= MIN_LANG_PROBABILITY


def _block_words(model, chunk: np.ndarray, offset: float, lang: str | None) -> list[Word]:
    segments, _ = model.transcribe(chunk, language=lang, word_timestamps=True,
                                   condition_on_previous_text=False)
    words = []
    for segment in segments:
        if segment.no_speech_prob > 0.6:
            continue
        for w in segment.words or []:
            text = w.word.strip()
            if text and w.probability >= MIN_PROBABILITY:
                words.append(Word(offset + float(w.start), offset + float(w.end), text))
    return words


def transcribe_words(path: str) -> list[Word]:
    """Palavras faladas pelo streamer no clip, em ordem, com início e fim em segundos."""
    if not available():
        return []
    audio = _audio(path)
    if len(audio) < RATE:
        return []
    lang = CAPTION_LANG or None
    words: list[Word] = []
    with _lock:   # um clip por vez: o modelo já usa todos os núcleos
        model = _load()
        for start, end in speech_blocks(_speech_spans(audio)):
            pad = 0.2
            a, b = max(0, int((start - pad) * RATE)), min(len(audio), int((end + pad) * RATE))
            chunk = audio[a:b]
            if len(chunk) < RATE // 2:
                continue
            if lang and not _in_language(model, chunk, lang):
                continue          # fala em outro idioma: é o jogo (ou um vídeo), não o streamer
            words += _block_words(model, chunk, a / RATE, lang)
    return words


def transcribe(path: str) -> str | None:
    """Texto corrido (usado para sugerir o título)."""
    return " ".join(w.text for w in transcribe_words(path)) or None
