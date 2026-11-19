"""Orquestra um clip: detectar câmera -> título -> render vertical."""
import os
import sys
from pathlib import Path

from .config import Rect
from .detect.webcam import detect_webcam
from .render.compose import compose


def _log(msg: str) -> None:
    print(f"[viceclips] {msg}", file=sys.stderr)


def ai_titles_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _words(src: str) -> list:
    """Fala do clip com tempo por palavra; vazio se a transcrição não estiver disponível."""
    try:
        from .ai.transcribe import transcribe_words
        return transcribe_words(src)
    except Exception as exc:  # legenda nunca deve derrubar o render
        _log(f"transcrição falhou ({exc}); seguindo sem legenda.")
        return []


def _auto_title(src: str, words: list) -> str | None:
    if not ai_titles_enabled():
        return None  # sem chave da Anthropic o clip simplesmente sai sem título
    try:
        from .ai.title import generate_title
        return generate_title(src, " ".join(w.text for w in words) or None)
    except Exception as exc:  # o título nunca deve derrubar o render
        _log(f"título automático falhou ({exc}); seguindo sem título.")
        return None


def build_clip(src: str, out: str | Path, title: str | None = None,
               cam: Rect | None = None, credit: str | None = None,
               log=_log, known_cam: Rect | None = None) -> tuple[str, str | None, Rect | None]:
    """Gera o clip vertical; devolve (arquivo, título usado, câmera detectada neste clip).

    `known_cam` é a posição da câmera já vista nessa mesma live: vale quando a detecção
    falha (streamer saiu da frente, por exemplo), para o layout não mudar de um clip a outro.
    """
    detected = None
    if cam is None:
        log("Procurando a câmera")
        detected = detect_webcam(src)
        cam = detected or known_cam
        _log(f"câmera detectada em {detected}" if detected else
             f"câmera não detectada; usando a posição já conhecida {known_cam}" if known_cam else
             "câmera não encontrada: layout só gameplay.")
    log("Transcrevendo a fala")
    words = _words(src)
    if title is None and ai_titles_enabled():
        log("Gerando título")
        title = _auto_title(src, words)
    if title:
        _log(f"título: {title}")
    log("Renderizando")
    return compose(src, str(out), cam=cam, title=title, credit=credit, words=words), title, detected


def make_clip(src: str, out: str | Path, title: str | None = None,
              cam: Rect | None = None, credit: str | None = None) -> str:
    """Gera o clip vertical a partir de um trecho horizontal já recortado no tempo."""
    return build_clip(src, out, title=title, cam=cam, credit=credit, log=lambda _: None)[0]
