"""Constantes de layout e identidade visual (preto + rosa)."""
import os
from dataclasses import dataclass
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
CAM_H = 608            # painel da câmera (16:9 em 1080 de largura)
DIVIDER_H = 6          # faixa rosa entre câmera e gameplay
GAME_H = OUT_H - CAM_H - DIVIDER_H

PINK = "#FF2E9A"
BLACK = "#0B0B0F"
WHITE = "#FFFFFF"

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

# Marca d'água do canal: arquivo PNG com transparência ("" desliga) e opacidade de 0 a 1.
LOGO_PATH = os.environ.get("VICECLIPS_LOGO", str(Path(__file__).parent / "render" / "assets" / "logo.png"))
LOGO_OPACITY = float(os.environ.get("VICECLIPS_LOGO_OPACITY", "0.7"))

WORK_DIR = Path(os.environ.get("VICECLIPS_WORK", "work"))
OUT_DIR = Path(os.environ.get("VICECLIPS_OUT", "out"))
TITLE_LANG = os.environ.get("VICECLIPS_TITLE_LANG", "pt-BR")
TITLE_MODEL = os.environ.get("VICECLIPS_TITLE_MODEL", "claude-opus-5-5")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, text: str) -> "Rect":
        x, y, w, h = (int(v) for v in text.split(","))
        return cls(x, y, w, h)


def font_path() -> str:
    env = os.environ.get("VICECLIPS_FONT")
    for p in ([env] if env else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    raise FileNotFoundError("Nenhuma fonte bold encontrada; defina VICECLIPS_FONT.")
