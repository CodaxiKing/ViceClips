"""Registro do que o servidor está fazendo, para mostrar na tela "Atividade".

Fica só na memória (os últimos 300 acontecimentos): serve para acompanhar ao vivo,
não é histórico.
"""
import threading
import time
from collections import deque

_events: deque = deque(maxlen=300)
_lock = threading.Lock()
_counter = 0


def log(text: str, level: str = "info", source: str | None = None) -> None:
    """level: info (passo normal), ok (algo ficou pronto), warn (descartado/aviso), error."""
    global _counter
    with _lock:
        _counter += 1
        _events.append({"id": _counter, "ts": time.time(), "level": level,
                        "text": text, "source": source})


def recent(after: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _events if e["id"] > after]


def clear() -> None:
    with _lock:
        _events.clear()
