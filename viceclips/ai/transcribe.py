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
MIN_LANG_PROBABILITY = 0.6  # certeza mínima de que o trecho está no idioma do streamer
LANG_WINDOW = 4.0          # o idioma é conferido em janelas curtas, não no bloco inteiro
# Palavras muito comuns de cada idioma: segunda conferência, feita no texto já transcrito.
FOREIGN_WORDS = {
    "pt": set("the you and is are was were what that this with for have has not don't can't i'm it's "
              "he she they them his her your my me we of to in on at be do did will would just right "
              "now know want got get going gonna yeah okay hey there here him out up about from".split()),
}
OWN_WORDS = {
    "pt": set("o a os as um uma de do da dos das que não nao é e eu você voce ele ela nós isso essa esse "
              "esta está tá tô para pra por com sem mais mas muito aqui aí ai lá já vai vou tem tinha foi "
              "ser era meu minha seu sua né cara mano gente agora então entao também tambem se na no nas nos "
              "como quando porque onde quem olha vamos bora".split()),
}
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


def _language_windows(model, audio: np.ndarray, start: float, end: float, lang: str) -> list[tuple[float, float]]:
    """Pedaços de [start, end] em que a fala está no idioma do streamer.

    Conferir janelas curtas separa o streamer do jogo mesmo quando falam um logo após o
    outro; no bloco inteiro, quem falasse mais levava o bloco todo.
    """
    count = max(1, round((end - start) / LANG_WINDOW))
    size = (end - start) / count
    kept = []
    for i in range(count):
        a, b = start + i * size, start + (i + 1) * size
        chunk = audio[int(a * RATE): int(b * RATE)]
        if len(chunk) < RATE // 2:
            continue
        top, _, ranked = model.detect_language(chunk)
        if top == lang and dict(ranked).get(lang, 0.0) >= MIN_LANG_PROBABILITY:
            if kept and abs(kept[-1][1] - a) < 0.01:
                kept[-1] = (kept[-1][0], b)          # emenda janelas vizinhas
            else:
                kept.append((a, b))
    return kept


def looks_foreign(words: list, lang: str) -> bool:
    """O texto transcrito tem cara de outro idioma? (rede de segurança depois do áudio)"""
    tokens = [w.text.lower().strip(".,!?;:\"'") for w in words]
    tokens = [t for t in tokens if t]
    if len(tokens) < 3:
        return False
    foreign = sum(t in FOREIGN_WORDS.get(lang, ()) for t in tokens)
    own = sum(t in OWN_WORDS.get(lang, ()) for t in tokens)
    return foreign >= 2 and foreign > own


def _transcribe_piece(model, audio: np.ndarray, start: float, end: float, lang: str | None) -> list[Word]:
    pad = 0.15
    a, b = max(0, int((start - pad) * RATE)), min(len(audio), int((end + pad) * RATE))
    segments, _ = model.transcribe(audio[a:b], language=lang, word_timestamps=True,
                                   condition_on_previous_text=False)
    words = []
    for segment in segments:
        if segment.no_speech_prob > 0.6:
            continue
        piece = []
        for w in segment.words or []:
            text = w.word.strip()
            if text and w.probability >= MIN_PROBABILITY:
                piece.append(Word(a / RATE + float(w.start), a / RATE + float(w.end), text))
        if lang and looks_foreign(piece, lang):
            continue
        words += piece
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
            if end - start < 0.4:
                continue
            # Só é transcrito o que está no idioma do streamer; a fala do jogo (ou de um
            # vídeo), em outro idioma, nem chega ao transcritor, que senão a "traduziria".
            pieces = _language_windows(model, audio, start, end, lang) if lang else [(start, end)]
            for a, b in pieces:
                words += _transcribe_piece(model, audio, a, b, lang)
    return words


def transcribe(path: str) -> str | None:
    """Texto corrido (usado para sugerir o título)."""
    return " ".join(w.text for w in transcribe_words(path)) or None
