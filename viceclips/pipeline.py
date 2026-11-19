"""Orquestra um clip: detectar câmera -> título -> render vertical."""
import os
import sys
from pathlib import Path

from .config import Rect
from .detect.webcam import detect_webcam
from .render.compose import compose


def _log(msg: str) -> None:
    print(f"[viceclips] {msg}", file=sys.stderr)


def _auto_title(src: str) -> str | None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        _log("sem credencial Anthropic: clip sai sem título (use --title).")
        return None
    try:
        from .ai.title import generate_title
        from .ai.transcribe import transcribe
        return generate_title(src, transcribe(src))
    except Exception as exc:  # o título nunca deve derrubar o render
        _log(f"título automático falhou ({exc}); seguindo sem título.")
        return None


def build_clip(src: str, out: str | Path, title: str | None = None,
               cam: Rect | None = None, credit: str | None = None,
               log=_log) -> tuple[str, str | None, Rect | None]:
    """Gera o clip vertical; devolve (arquivo, título usado, câmera usada)."""
    if cam is None:
        log("Procurando a câmera")
        cam = detect_webcam(src)
        _log(f"câmera detectada em {cam}" if cam else "câmera não encontrada: layout só gameplay.")
    if title is None:
        log("Gerando título")
        title = _auto_title(src)
    if title:
        _log(f"título: {title}")
    log("Renderizando")
    return compose(src, str(out), cam=cam, title=title, credit=credit), title, cam


def make_clip(src: str, out: str | Path, title: str | None = None,
              cam: Rect | None = None, credit: str | None = None) -> str:
    """Gera o clip vertical a partir de um trecho horizontal já recortado no tempo."""
    return build_clip(src, out, title=title, cam=cam, credit=credit, log=lambda _: None)[0]
