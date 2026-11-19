from viceclips import config
from viceclips.config import Rect
from viceclips.media import parse_time
from viceclips.render.compose import game_crop

import numpy as np


def test_parse_time():
    assert parse_time("90") == 90
    assert parse_time("1:30") == 90
    assert parse_time("1:02:03") == 3723


def test_game_crop_keeps_target_aspect_and_stays_in_frame():
    g = game_crop(1920, 1080, config.OUT_W, config.GAME_H, None)
    assert g.h == 1080 and abs(g.w / g.h - config.OUT_W / config.GAME_H) < 0.01
    assert g.x >= 0 and g.x + g.w <= 1920


def test_game_crop_shifts_away_from_overlapping_cam():
    centered = game_crop(1920, 1080, config.OUT_W, config.GAME_H, None)
    left_cam = game_crop(1920, 1080, config.OUT_W, config.GAME_H, Rect(300, 0, 400, 225))
    right_cam = game_crop(1920, 1080, config.OUT_W, config.GAME_H, Rect(1300, 0, 400, 225))
    corner_cam = game_crop(1920, 1080, config.OUT_W, config.GAME_H, Rect(0, 0, 300, 170))
    assert left_cam.x > centered.x
    assert right_cam.x < centered.x
    assert corner_cam == centered




def test_camera_border_search_prefers_the_innermost_clear_line():
    from viceclips.detect.webcam import _find_line

    profile = np.full(400, 10.0, np.float32)
    profile[100] = 200      # moldura da câmera
    profile[40] = 220       # outra linha fixa, mais longe do rosto (HUD do jogo)
    assert _find_line(profile, 0, 150, "hi") == 100      # rosto à direita: fica com a de dentro
    profile[300], profile[360] = 200, 220
    assert _find_line(profile, 250, 400, "lo") == 300    # rosto à esquerda: idem


def test_camera_border_search_gives_up_without_a_clear_line():
    from viceclips.detect.webcam import _find_line

    flat = np.full(200, 10.0, np.float32)
    assert _find_line(flat, 0, 200, "hi") is None        # nada se destaca
    flat[50] = 25
    assert _find_line(flat, 0, 200, "hi") is None        # destaque fraco não conta como borda
    assert _find_line(flat, 190, 191, "hi") is None      # faixa de busca vazia


def test_logo_watermark_is_semi_transparent_and_can_be_turned_off(tmp_path, monkeypatch):
    from PIL import Image
    from viceclips.render.title_card import LOGO_MARGIN, LOGO_SIZE, render_overlay

    corner = (config.OUT_W - LOGO_MARGIN - LOGO_SIZE // 2, 400 + LOGO_SIZE // 2)   # centro da logo
    alpha_at_logo = lambda path: Image.open(path).getchannel("A").getpixel(corner)

    with_logo = render_overlay(None, None, 600, str(tmp_path / "a.png"), logo_y=400)
    assert 100 < alpha_at_logo(with_logo) < 255            # visível, mas não opaca
    assert Image.open(with_logo).getchannel("A").getpixel((540, 1200)) == 0   # resto da tela livre

    monkeypatch.setattr(config, "LOGO_OPACITY", 0.0)
    assert alpha_at_logo(render_overlay(None, None, 600, str(tmp_path / "b.png"), logo_y=400)) == 0
