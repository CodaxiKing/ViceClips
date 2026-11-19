"""Título curto do clip via Claude: frames do momento + transcrição (se houver)."""
import base64

from .. import config
from ..media import frame_jpeg, probe

SYSTEM = """Você escreve títulos para clips verticais de lives de GTA VI.
Recebe alguns frames do clip (em ordem) e, às vezes, a transcrição da fala do streamer.
Responda apenas com o título: no máximo 5 palavras, descrevendo o que aconteceu de
mais marcante no jogo, no estilo "GTA 6 Alien Abduction". Sem aspas, sem hashtags,
sem emoji, sem ponto final. Não invente nada que os frames ou a fala não mostrem."""


def generate_title(clip_path: str, transcript: str | None = None,
                   lang: str | None = None) -> str | None:
    """Retorna o título, ou None se o modelo recusar."""
    import anthropic

    duration = probe(clip_path)["duration"]
    content = []
    for frac in (0.25, 0.5, 0.75, 0.9):
        jpeg = frame_jpeg(clip_path, duration * frac)
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode()}})
    text = f"Idioma do título: {lang or config.TITLE_LANG}."
    if transcript:
        text += f"\n\nTranscrição:\n{transcript}"
    content.append({"type": "text", "text": text})

    response = anthropic.Anthropic().beta.messages.create(
        model=config.TITLE_MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        return None
    title = "".join(b.text for b in response.content if b.type == "text").strip()
    return title.splitlines()[0].strip(' "') if title else None
