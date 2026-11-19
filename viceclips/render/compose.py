"""Monta o vídeo vertical final com ffmpeg.

Com câmera:  [ câmera 1080x608 ] + faixa rosa + [ gameplay 1080x1306 ], título sobre o topo do gameplay.
Sem câmera:  gameplay em 9:16 ocupando a tela toda, título no topo.
"""
import tempfile
from pathlib import Path

from .. import config
from ..config import Rect
from ..media import probe, run
from .captions import subtitles_filter, write_ass
from .title_card import LOGO_MARGIN, LOGO_SIZE, render_overlay


def _even(v: float) -> int:
    return int(v) // 2 * 2


def game_crop(src_w: int, src_h: int, target_w: int, target_h: int, cam: Rect | None) -> Rect:
    """Recorte do gameplay na proporção alvo, centralizado e desviando da câmera."""
    gh = src_h
    gw = min(src_w, src_h * target_w / target_h)
    if gw == src_w:
        gh = src_w * target_h / target_w
    gx = (src_w - gw) / 2
    if cam and cam.x < gx + gw and cam.x + cam.w > gx:
        max_shift = src_w * 0.12
        if cam.x + cam.w / 2 < src_w / 2:
            gx = min(src_w - gw, gx + max_shift, max(gx, cam.x + cam.w))
        else:
            gx = max(0, gx - max_shift, min(gx, cam.x - gw))
    return Rect(_even(gx), _even((src_h - gh) / 2), _even(gw), _even(gh))


def compose(src: str, out: str, cam: Rect | None = None,
            title: str | None = None, credit: str | None = None,
            words: list | None = None) -> str:
    info = probe(src)
    W, H = info["width"], info["height"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    if cam:
        g = game_crop(W, H, config.OUT_W, config.GAME_H, cam)
        pad_h = config.CAM_H + config.DIVIDER_H
        graph = (
            f"[0:v]split[a][b];"
            f"[a]crop={_even(cam.w)}:{_even(cam.h)}:{cam.x}:{cam.y},"
            f"scale={config.OUT_W}:{config.CAM_H}:force_original_aspect_ratio=increase,"
            f"crop={config.OUT_W}:{config.CAM_H},"
            f"pad={config.OUT_W}:{pad_h}:0:0:color={config.PINK.replace('#', '0x')}[cam];"
            f"[b]crop={g.w}:{g.h}:{g.x}:{g.y},scale={config.OUT_W}:{config.GAME_H}[game];"
            f"[cam][game]vstack[base];"
        )
        title_y = pad_h + 28
        # Canto inferior direito do painel da câmera: longe do rosto, do título, das
        # legendas e dos botões que TikTok/Reels desenham por cima do vídeo.
        logo_y = config.CAM_H - LOGO_SIZE - LOGO_MARGIN
        credit_y = config.CAM_H - 58 - LOGO_MARGIN       # mesmo rodapé do painel, do lado esquerdo
    else:
        g = game_crop(W, H, config.OUT_W, config.OUT_H, None)
        graph = f"[0:v]crop={g.w}:{g.h}:{g.x}:{g.y},scale={config.OUT_W}:{config.OUT_H}[base];"
        title_y = 150
        title_y = config.SAFE_TOP + 90
        logo_y = credit_y = config.SAFE_TOP    # sem câmera: logo e crédito logo abaixo da barra do app

    with tempfile.TemporaryDirectory() as tmp:
        overlay = render_overlay(title, credit, title_y, f"{tmp}/overlay.png", logo_y=logo_y,
                                 credit_y=credit_y)
        graph += "[base][1:v]overlay=0:0"
        ass = write_ass(words, f"{tmp}/fala.ass") if words else None
        if ass:
            graph += "," + subtitles_filter(ass)
        graph += ",setsar=1,format=yuv420p[v]"
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-i", overlay,
             "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
             # Formato aceito sem recompressão forçada por TikTok, Reels e Shorts: 1080x1920 (9:16),
             # H.264 High, até 60 fps, taxa limitada, áudio AAC 48 kHz, índice no começo do arquivo.
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-level", "4.2",
             "-maxrate", "10M", "-bufsize", "20M", "-fpsmax", "60", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out])
    return out
