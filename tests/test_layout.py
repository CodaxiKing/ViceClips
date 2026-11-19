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


