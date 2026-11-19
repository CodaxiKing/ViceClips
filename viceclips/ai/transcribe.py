"""Transcrição da fala do streamer (opcional; melhora o título e, depois, legendas)."""


def transcribe(path: str, model_size: str = "small") -> str | None:
    """Texto falado no clip, ou None se faster-whisper não estiver instalado."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    model = WhisperModel(model_size, compute_type="int8")
    segments, _ = model.transcribe(path, vad_filter=True)
    return " ".join(s.text.strip() for s in segments) or None
