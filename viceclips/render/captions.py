"""Legendas palavra por palavra: frases curtas na tela, com a palavra falada em rosa.

Gera um arquivo ASS que o ffmpeg queima no vídeo (filtro `subtitles`).
"""
import re
from pathlib import Path

from .. import config

MAX_WORDS = 3          # palavras por vez na tela
MAX_CHARS = 18
MAX_GAP = 0.6          # pausa que quebra a frase
HOLD = 0.25            # quanto a última palavra fica na tela depois de dita
CAPTION_Y = 1420       # centro vertical da legenda (terço de baixo do gameplay)
FONT_SIZE = 82


def _ass_color(hex_color: str) -> str:
    r, g, b = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H00{b}{g}{r}".upper()


def _clean(text: str) -> str:
    text = re.sub(r"[{}\\]", "", text).strip(" ,.;:\"“”…")
    return text.upper()


def _timestamp(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def group_words(words: list) -> list[list]:
    """Quebra a fala em grupos curtos, respeitando pausas e fim de frase."""
    groups, current = [], []
    for word in words:
        if not _clean(word.text):
            continue
        too_long = sum(len(_clean(w.text)) + 1 for w in current) + len(_clean(word.text)) > MAX_CHARS
        paused = current and word.start - current[-1].end > MAX_GAP
        if current and (len(current) >= MAX_WORDS or too_long or paused):
            groups.append(current)
            current = []
        current.append(word)
        if word.text.rstrip().endswith((".", "!", "?")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def write_ass(words: list, out_path: str) -> str | None:
    """Escreve o .ass; devolve None se não há fala para legendar."""
    groups = group_words(words)
    if not groups:
        return None
    white, pink = _ass_color(config.WHITE), _ass_color(config.PINK)
    font_name = "Inter ExtraBold" if "Inter" in config.font_path() else "DejaVu Sans"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {config.OUT_W}", f"PlayResY: {config.OUT_H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Fala,{font_name},{FONT_SIZE},{white},{white},&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,7,0,5,40,40,0,1", "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for g, group in enumerate(groups):
        next_start = groups[g + 1][0].start if g + 1 < len(groups) else None
        for i, word in enumerate(group):
            if i + 1 < len(group):
                end = group[i + 1].start
            else:
                end = word.end + HOLD
                if next_start is not None:
                    end = min(end, next_start)
            if end <= word.start:
                end = word.start + 0.08
            parts = [f"{{\\c{pink}&}}{_clean(w.text)}{{\\c{white}&}}" if j == i else _clean(w.text)
                     for j, w in enumerate(group)]
            text = f"{{\\pos({config.OUT_W // 2},{CAPTION_Y})}}" + " ".join(parts)
            lines.append(f"Dialogue: 0,{_timestamp(word.start)},{_timestamp(end)},Fala,,0,0,0,,{text}")
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def subtitles_filter(ass_path: str) -> str:
    """Trecho do filtergraph do ffmpeg que queima o .ass."""
    def esc(path: str) -> str:
        return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fonts = str(Path(config.font_path()).parent)
    return f"subtitles=filename='{esc(ass_path)}':fontsdir='{esc(fonts)}'"
