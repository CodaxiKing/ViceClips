from viceclips import config
from viceclips.config import Rect
from viceclips.highlights import audio
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


def test_find_highlights_picks_the_loud_second(monkeypatch):
    rms = np.full(120, 100.0, np.float32)
    rms[60] = 5000
    monkeypatch.setattr(audio, "loudness_per_second", lambda _: rms)
    windows = audio.find_highlights("x", count=3, pre=20, post=10)
    assert len(windows) == 1
    start, end, _ = windows[0]
    assert start <= 60 <= end and end - start <= 31


def test_peak_detector_flags_spike_once_and_respects_warmup_and_cooldown():
    from viceclips.highlights.live_watch import PeakDetector

    d = PeakDetector(warmup=45, cooldown=60)
    assert d.feed([100] * 20 + [5000] + [100] * 29) == []      # pico durante o aquecimento
    assert d.feed([100] * 30) == []                            # volume normal
    assert d.feed([110, 90, 6000, 100]) == [82]                # pico de verdade
    assert d.feed([6000, 100, 100]) == []                      # logo depois: cooldown
    assert d.feed([100] * 70 + [7000]) == [157]                # passou o cooldown


def test_peak_detector_ignores_steady_loud_audio():
    from viceclips.highlights.live_watch import PeakDetector

    d = PeakDetector()
    assert d.feed([3000 + (i % 7) * 20 for i in range(300)]) == []
