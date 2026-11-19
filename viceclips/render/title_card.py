"""Gera o PNG transparente (1080x1920) com título, faixa rosa e crédito."""
from PIL import Image, ImageDraw, ImageFont

from .. import config

MARGIN = 60
MAX_LINES = 2


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


def _printable(text: str) -> str:
    # A fonte do título não tem emoji; remove o que estiver fora do plano básico.
    return " ".join("".join(c for c in text if ord(c) < 0x2190).split())


def render_overlay(title: str | None, credit: str | None, title_y: int, out_path: str) -> str:
    img = Image.new("RGBA", (config.OUT_W, config.OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_file = config.font_path()

    if title and _printable(title):
        title = _printable(title)
        max_w = config.OUT_W - 2 * MARGIN
        for size in range(84, 40, -4):
            font = ImageFont.truetype(font_file, size)
            lines = _wrap(draw, title, font, max_w)
            if len(lines) <= MAX_LINES and all(draw.textlength(l, font=font) <= max_w for l in lines):
                break
        y = title_y
        for line in lines[:MAX_LINES]:
            draw.text((config.OUT_W / 2, y), line, font=font, anchor="ma",
                      fill=config.WHITE, stroke_width=max(4, size // 10), stroke_fill=config.BLACK)
            y += int(size * 1.18)
        bar_w = 180
        draw.rounded_rectangle(
            [(config.OUT_W - bar_w) / 2, y + 12, (config.OUT_W + bar_w) / 2, y + 22],
            radius=5, fill=config.PINK)

    if credit:
        font = ImageFont.truetype(font_file, 34)
        tw = draw.textlength(credit, font=font)
        x0, y0 = 28, 28
        draw.rounded_rectangle([x0, y0, x0 + tw + 36, y0 + 58], radius=29, fill=config.BLACK + "CC")
        draw.text((x0 + 18, y0 + 29), credit, font=font, anchor="lm", fill=config.PINK)

    img.save(out_path)
    return out_path
