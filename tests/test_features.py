"""Legendas, validade dos clips, avaliações, várias lives, canais e avisos."""
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from viceclips import config
from viceclips.ai.transcribe import Word
from viceclips.render import captions
from viceclips.web import app as web
from viceclips.web import lives, push

client = TestClient(web.app)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(web, "PASSWORD", "")
    web._jobs.clear()
    yield
    from viceclips.highlights import analysis
    analysis.MIN_SCORE = analysis.DEFAULT_MIN_SCORE
    for session_id in list(web.manager.sessions):
        web.manager.remove(session_id)


def make_clip(name: str, age: float = 0, **extra) -> None:
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in (".mp4", ".jpg"):
        (config.OUT_DIR / f"{name}{suffix}").write_bytes(b"x")
    meta = {"name": name, "title": None, "credit": None, "source": "s", "kind": None,
            "score": None, "rating": None, "duration": 20.0, "created": time.time() - age, **extra}
    (config.OUT_DIR / f"{name}.json").write_text(json.dumps(meta))


# ---------- legendas ----------

def words(*items):
    return [Word(start, end, text) for start, end, text in items]


def test_captions_group_short_phrases_and_break_on_pause_and_sentence_end():
    spoken = words((0.0, 0.3, "Olha"), (0.3, 0.6, "isso"), (0.6, 0.9, "aqui"), (0.9, 1.2, "cara."),
                   (1.3, 1.6, "Não"), (3.0, 3.4, "acredito"))
    groups = [[w.text for w in g] for g in captions.group_words(spoken)]
    assert groups == [["Olha", "isso", "aqui"], ["cara."], ["Não"], ["acredito"]]


def test_captions_highlight_one_word_at_a_time_in_pink(tmp_path):
    path = captions.write_ass(words((1.0, 1.4, "Meu"), (1.4, 1.9, "deus,"), (1.9, 2.5, "{cara}")),
                              str(tmp_path / "fala.ass"))
    events = [line for line in open(path, encoding="utf-8") if line.startswith("Dialogue")]
    assert len(events) == 3                                   # um evento por palavra falada
    pink = captions._ass_color(config.PINK)
    assert events[0].count(pink) == 1 and f"{pink}&}}MEU" in events[0]
    assert f"{pink}&}}DEUS" in events[1] and "DEUS," not in events[1]
    assert "{CARA}" not in events[2] and "CARA" in events[2]   # chaves não viram comando do ASS
    assert "0:00:01.00,0:00:01.40" in events[0]               # dura até a próxima palavra


def test_no_speech_means_no_caption_file(tmp_path):
    assert captions.write_ass([], str(tmp_path / "fala.ass")) is None


def test_render_burns_captions_into_the_video(tmp_path):
    from viceclips.render.compose import compose

    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    spoken = words((0.2, 1.8, "TESTE"))
    plain, captioned = tmp_path / "a.mp4", tmp_path / "b.mp4"
    compose(str(src), str(plain))
    compose(str(src), str(captioned), words=spoken)

    def brightness(video) -> float:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
                              "-vf", f"crop=1080:200:0:{captions.CAPTION_Y - 100},format=gray",
                              "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        return sum(raw) / len(raw)

    assert brightness(plain) < 1                # fundo preto, sem nada
    assert brightness(captioned) > 2            # a palavra apareceu na faixa da legenda


# ---------- validade ----------

def test_clips_are_deleted_after_the_ttl_and_fresh_ones_stay():
    make_clip("clip_velho", age=web.CLIP_TTL + 30)
    make_clip("clip_novo", age=60)
    web.sweep()
    assert not list(config.OUT_DIR.glob("clip_velho.*"))
    assert len(list(config.OUT_DIR.glob("clip_novo.*"))) == 3
    listed = client.get("/api/clips").json()
    assert [c["name"] for c in listed] == ["clip_novo"]
    assert web.CLIP_TTL - 70 <= listed[0]["expires_in"] <= web.CLIP_TTL - 55


def test_ttl_zero_keeps_clips_forever(monkeypatch):
    monkeypatch.setattr(web, "CLIP_TTL", 0)
    make_clip("clip_antigo", age=999999)
    web.sweep()
    assert client.get("/api/clips").json()[0]["expires_in"] is None


def test_sweep_removes_only_stale_leftovers():
    config.WORK_DIR.mkdir(parents=True)
    stale, fresh, keep = (config.WORK_DIR / n for n in ("upload_a.mp4", "live_b.mp4", "jobs.json"))
    for path in (stale, fresh, keep):
        path.write_text("x")
    web.sweep(now=time.time() + web.STALE_WORK_SECONDS + 60)
    assert not stale.exists() and not fresh.exists() and keep.exists()
    fresh.write_text("x")
    web.sweep()
    assert fresh.exists()


# ---------- avaliações ----------

def test_rating_is_saved_and_survives_clip_deletion():
    make_clip("clip_a", kind="engraçado", score=12.5)
    make_clip("clip_b", kind="impactante", score=7.0)
    assert client.post("/api/clips/clip_a/rating", json={"good": True}).json()["rating"] == "bom"
    client.post("/api/clips/clip_b/rating", json={"good": True})
    summary = client.post("/api/clips/clip_b/rating", json={"good": False}).json()   # mudou de ideia
    assert (summary["good"], summary["bad"]) == (1, 1)
    assert {c["name"]: c["rating"] for c in client.get("/api/clips").json()} == {"clip_a": "bom", "clip_b": "ruim"}

    client.delete("/api/clips/clip_a")
    after = client.get("/api/ratings").json()
    assert (after["good"], after["bad"]) == (1, 1) and after["calibration"]["needed"] == 13
    record = json.loads(web._ratings_file().read_text().splitlines()[0])
    assert record["kind"] == "engraçado" and record["score"] == 12.5 and record["auto"] is True
    assert client.post("/api/clips/clip_a/rating", json={"good": True}).status_code == 404


# ---------- várias lives e canais ----------

class FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def fake_record(monkeypatch):
    monkeypatch.setattr(lives.live, "record", lambda url, directory: FakeProc())


def test_several_lives_at_once_with_a_limit(fake_record, monkeypatch):
    monkeypatch.setattr(lives, "MAX_LIVES", 2)
    start = lambda url: client.post("/api/live/start", json={"url": url, "auto": False})
    assert start("https://www.twitch.tv/canal_a").status_code == 200
    assert start("https://m.twitch.tv/canal_a?x=1").status_code == 409      # mesma live, outro formato de link
    status = start("https://www.twitch.tv/canal_b").json()
    assert [s["name"] for s in status["sessions"]] == ["canal_a", "canal_b"]
    blocked = start("https://www.twitch.tv/canal_c")
    assert blocked.status_code == 409 and "Limite de 2" in blocked.json()["detail"]

    first = status["sessions"][0]["id"]
    stopped = client.post(f"/api/live/{first}/stop").json()["sessions"][0]
    assert stopped["running"] is False and stopped["failed"] is False
    assert start("https://www.twitch.tv/canal_c").status_code == 200        # abriu vaga
    assert len(client.delete(f"/api/live/{first}").json()["sessions"]) == 2
    assert client.post(f"/api/live/{first}/stop").status_code == 404


def test_registered_channel_starts_recording_when_it_goes_live(fake_record, monkeypatch):
    online = {"https://www.twitch.tv/canal_a": False}
    monkeypatch.setattr(lives.vod, "probe", lambda url: {"is_live": online[url], "duration": None})
    monkeypatch.setattr(lives.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    client.post("/api/channels", json={"url": "https://www.twitch.tv/canal_a", "credit": "@a"})
    assert client.post("/api/channels", json={"url": "https://twitch.tv/canal_a"}).status_code == 409

    web.manager.check_channels()
    status = client.get("/api/live").json()
    assert status["sessions"] == [] and status["channels"][0]["live"] is False

    online["https://www.twitch.tv/canal_a"] = True
    web.manager.check_channels()
    web.manager.check_channels()                       # segunda rodada não duplica a gravação
    status = client.get("/api/live").json()
    assert [(s["name"], s["origin"]) for s in status["sessions"]] == [("canal_a", "canal")]
    assert status["channels"][0]["live"] is True

    # A live acabou: a sessão do canal sai da lista para recomeçar na próxima transmissão.
    session = next(iter(web.manager.sessions.values()))
    # O gravador cai com a live ainda no ar: é retomado, a sessão continua.
    session.proc.returncode = 0
    session._check()
    assert session.running and session.reconnects == 1

    # Agora a live acabou de verdade (o canal não abre mais).
    def offline(url, directory):
        raise RuntimeError("O canal está offline.")
    monkeypatch.setattr(lives.live, "record", offline)
    session.proc.returncode = 0
    session._check()
    assert not session.running and not session.failed
    session.ended_at = time.time() - 200
    web.manager.reap()
    assert client.get("/api/live").json()["sessions"] == []

    client.post("/api/channels/remove", json={"url": "https://www.twitch.tv/canal_a"})
    assert client.get("/api/live").json()["channels"] == []


# ---------- avisos ----------

def test_push_only_accepts_browser_push_services():
    assert push.allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert push.allowed_endpoint("https://web.push.apple.com/abc")
    assert push.allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/abc")
    assert not push.allowed_endpoint("http://fcm.googleapis.com/fcm/send/abc")       # sem https
    assert not push.allowed_endpoint("https://169.254.169.254/latest/meta-data")
    assert not push.allowed_endpoint("https://evil.example/fcm.googleapis.com")
    res = client.post("/api/push/subscribe", json={"endpoint": "https://evil.example/x",
                                                   "keys": {"p256dh": "a", "auth": "b"}})
    assert res.status_code == 422


def test_push_subscription_lifecycle_and_expired_ones_are_dropped(monkeypatch):
    sent = []
    monkeypatch.setattr(push.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    monkeypatch.setattr(push, "_send_one", lambda sub, payload: sent.append((sub["endpoint"], json.loads(payload))) or
                        (410 if "morto" in sub["endpoint"] else 201))
    key = client.get("/api/push/key").json()["key"]
    assert len(push._unb64(key)) == 65 and client.get("/api/push/key").json()["key"] == key   # chave estável
    for name in ("vivo", "morto"):
        client.post("/api/push/subscribe", json={"endpoint": f"https://fcm.googleapis.com/fcm/send/{name}",
                                                 "keys": {"p256dh": "a", "auth": "b"}})
    sent.clear()
    push.notify("Clip pronto", "teste")
    assert len(sent) == 1 and sent[0][1] == {"title": "Clip pronto", "body": "teste", "url": "/#clips"}
    assert [s["endpoint"].rsplit("/", 1)[-1] for s in push.subscriptions()] == ["vivo"]


# ---------- legenda só do streamer ----------

def test_speech_is_grouped_into_blocks_split_by_pauses_and_length():
    from viceclips.ai.transcribe import speech_blocks

    spans = [(0.0, 2.0), (2.5, 4.0), (6.0, 7.0), (7.2, 14.0), (14.5, 23.0)]
    assert speech_blocks(spans) == [(0.0, 4.0), (6.0, 14.0), (14.5, 23.0)]


def test_only_speech_in_the_streamer_language_becomes_caption(monkeypatch):
    import numpy as np
    from viceclips.ai import transcribe as t

    class Segment:
        no_speech_prob = 0.0

        def __init__(self, text):
            self.words = [type("W", (), {"word": text, "start": 0.5, "end": 0.9, "probability": 0.9})()]

    class FakeModel:
        def detect_language(self, chunk):
            # o "jogo" é o bloco mais alto; o streamer, o mais baixo
            lang = "en" if float(np.abs(chunk).max()) > 0.5 else "pt"
            return lang, 0.95, [(lang, 0.95), ("pt" if lang == "en" else "en", 0.02)]

        def transcribe(self, chunk, language=None, **_):
            assert language == "pt"
            return [Segment("oi")], None

    monkeypatch.setattr(t, "available", lambda: True)
    monkeypatch.setattr(t, "_load", lambda: FakeModel())
    monkeypatch.setattr(t, "_speech_spans", lambda audio: [(1.0, 3.0), (6.0, 8.0)])
    audio = np.full(10 * t.RATE, 0.1, np.float32)
    audio[5 * t.RATE:] = 0.9                                    # segundo bloco: fala do jogo
    monkeypatch.setattr(t, "_audio", lambda path: audio)

    words = t.transcribe_words("clip.mp4")
    assert [w.text for w in words] == ["oi"] and 1.0 <= words[0].start <= 2.0   # só o bloco em português

    monkeypatch.setattr(t, "CAPTION_LANG", "")                  # filtro desligado: legenda tudo
    monkeypatch.setattr(FakeModel, "transcribe", lambda self, chunk, language=None, **_: ([Segment("x")], None))
    assert len(t.transcribe_words("clip.mp4")) == 2


# ---------- favoritos, zip, calibração, saúde, live gravada ----------

def test_favorite_is_not_deleted_and_loses_the_countdown():
    make_clip("clip_fav", age=web.CLIP_TTL + 500)
    make_clip("clip_comum", age=web.CLIP_TTL + 500)
    client.post("/api/clips/clip_fav/favorite", json={"on": True})
    web.sweep()
    listed = client.get("/api/clips").json()
    assert [(c["name"], c["favorite"], c["expires_in"]) for c in listed] == [("clip_fav", True, None)]

    client.post("/api/clips/clip_fav/favorite", json={"on": False})      # prazo recomeça agora
    web.sweep()
    assert web.CLIP_TTL - 5 <= client.get("/api/clips").json()[0]["expires_in"] <= web.CLIP_TTL


def test_zip_download_of_all_or_only_favorites():
    import io
    import zipfile

    make_clip("clip_a", title="Fuga épica!")
    make_clip("clip_b", age=5)
    client.post("/api/clips/clip_b/favorite", json={"on": True})
    names = lambda res: zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert sorted(names(client.get("/api/clips/zip"))) == ["01 Fuga épica.mp4", "02 clip.mp4"]
    assert names(client.get("/api/clips/zip", params={"favorites": True})) == ["01 clip.mp4"]
    assert names(client.get("/api/clips/zip", params={"names": "clip_a"})) == ["01 Fuga épica.mp4"]
    assert client.get("/api/clips/zip", params={"names": "clip_nao_existe"}).status_code == 404
    assert not list(config.WORK_DIR.glob("zip_*"))                      # o arquivo temporário some


def ratings(good_scores, bad_scores):
    records = [{"name": f"g{i}", "good": True, "score": s, "auto": True} for i, s in enumerate(good_scores)]
    return records + [{"name": f"b{i}", "good": False, "score": s, "auto": True} for i, s in enumerate(bad_scores)]


def test_calibration_waits_for_enough_ratings_and_ignores_manual_clips():
    from viceclips.highlights import calibration

    few = calibration.suggest(ratings([9, 10], [5.5]))
    assert few["min_score"] == 5.0 and few["calibrated"] is False and few["needed"] == 12
    manual = [{"name": f"m{i}", "good": False, "score": None, "auto": False} for i in range(40)]
    assert calibration.suggest(manual)["rated"] == 0


def test_calibration_raises_the_bar_when_low_scores_are_the_bad_ones():
    from viceclips.highlights import calibration

    result = calibration.suggest(ratings([8, 9, 9.5, 10, 11, 12, 13, 14, 15, 16], [5.2, 5.5, 6, 6.5, 7, 7.5]))
    assert result["calibrated"] and result["min_score"] == 8.0        # corta todos os ruins, mantém os bons
    # reprovados espalhados por todas as notas: não há corte que ajude, fica como está
    mixed = calibration.suggest(ratings([6, 8, 10, 12, 14, 6.5, 9, 11, 13, 15], [6.2, 9.5, 12.5, 14.5, 10.5]))
    assert mixed["min_score"] <= 6.5


def test_calibration_never_drops_most_of_the_approved_clips_and_relaxes_when_all_good():
    from viceclips.highlights import analysis, calibration

    harsh = calibration.suggest(ratings([6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 20], [11, 12, 13, 14, 15]))
    kept = sum(s >= harsh["min_score"] for s in [6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 20])
    assert kept >= 7
    relaxed = calibration.apply(ratings([7 + i for i in range(16)], []))
    assert relaxed["min_score"] == 4.0 and analysis.MIN_SCORE == 4.0
    calibration.apply([])
    assert analysis.MIN_SCORE == analysis.DEFAULT_MIN_SCORE


def test_health_reports_resources_and_warns_about_backlog_and_errors():
    now = time.time()
    web._jobs.update({
        "ok": {"id": "ok", "label": "a", "status": "pronto", "created": now - 100, "started": now - 90, "finished": now - 60, "step": "Pronto", "error": None},
        **{f"w{i}": {"id": f"w{i}", "label": "b", "status": "fila", "created": now, "step": "Na fila", "error": None} for i in range(5)},
        **{f"e{i}": {"id": f"e{i}", "label": "live x", "status": "erro", "created": now - 10, "step": "Erro", "error": "falhou"} for i in range(3)},
    })
    health = client.get("/api/health").json()
    assert health["disk"]["total"] > health["disk"]["free"] > 0 and health["cpu"]["cores"] >= 1
    assert health["queue"]["waiting"] == 5 and health["renders"] == {"count": 1, "average": 30, "slowest": 30}
    assert len(health["errors"]) == 3 and health["errors"][0]["error"] == "falhou"
    text = " ".join(health["warnings"])
    assert "esperando na fila" in text and "falharam na última hora" in text


def test_recording_of_an_ongoing_stream_counts_as_a_video_not_as_live(monkeypatch):
    from viceclips.ingest import vod

    monkeypatch.setattr(vod, "_ytdlp", lambda args, timeout=None: "True\t19069.0\n")
    assert vod.probe("https://twitch.tv/videos/1") == {"is_live": False, "duration": 19069.0}
    monkeypatch.setattr(vod, "_ytdlp", lambda args, timeout=None: "True\tNA\n")
    assert vod.probe("https://twitch.tv/canal") == {"is_live": True, "duration": None}


def test_hls_playlist_parsing_picks_segment_times_and_header():
    from viceclips.ingest.hls import parse_playlist

    text = "\n".join(["#EXTM3U", '#EXT-X-MAP:URI="init-0.mp4"', "#EXTINF:10.000,", "0.mp4",
                      "#EXTINF:10.000,", "1.mp4", "#EXTINF:4.5,", "https://cdn.x/2.mp4", "#EXT-X-ENDLIST"])
    init, segments = parse_playlist(text, "https://cdn.x/vod/index.m3u8")
    assert init == "https://cdn.x/vod/init-0.mp4"
    assert segments == [("https://cdn.x/vod/0.mp4", 0.0, 10.0), ("https://cdn.x/vod/1.mp4", 10.0, 10.0),
                        ("https://cdn.x/2.mp4", 20.0, 4.5)]


def test_health_survives_files_vanishing_while_it_counts(monkeypatch):
    from pathlib import Path

    config.WORK_DIR.mkdir(parents=True)
    (config.WORK_DIR / "jobs.abc.tmp").write_text("x")
    (config.WORK_DIR / "fica.bin").write_bytes(b"12345")
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise FileNotFoundError(self)          # o arquivo temporário sumiu no meio da contagem
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["disk"]["work"] == 5


def test_static_files_are_always_revalidated():
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_streamer_and_game_in_the_same_block_are_separated(monkeypatch):
    import numpy as np
    from viceclips.ai import transcribe as t

    class Segment:
        no_speech_prob = 0.0

        def __init__(self, text):
            self.words = [type("W", (), {"word": text, "start": 0.2, "end": 0.6, "probability": 0.9})()]

    transcribed = []

    class FakeModel:
        def detect_language(self, chunk):
            lang = "en" if float(np.abs(chunk).mean()) > 0.5 else "pt"
            return lang, 0.9, [(lang, 0.9)]

        def transcribe(self, chunk, language=None, **_):
            transcribed.append(round(len(chunk) / t.RATE, 1))
            return [Segment("fala")], None

    audio = np.full(12 * t.RATE, 0.1, np.float32)
    audio[4 * t.RATE: 8 * t.RATE] = 0.9                   # 4-8 s: o jogo fala, colado na fala do streamer
    monkeypatch.setattr(t, "available", lambda: True)
    monkeypatch.setattr(t, "_load", lambda: FakeModel())
    monkeypatch.setattr(t, "_speech_spans", lambda a: [(0.0, 12.0)])     # um bloco só, sem pausa
    monkeypatch.setattr(t, "_audio", lambda path: audio)

    words = t.transcribe_words("clip.mp4")
    assert len(words) == 2                                 # antes e depois do jogo
    assert all(not 4.0 <= w.start < 8.0 for w in words)    # nada do trecho do jogo
    assert sum(transcribed) < 9                            # e o trecho do jogo nem foi transcrito


def test_text_that_reads_as_english_is_dropped_as_a_safety_net():
    from viceclips.ai.transcribe import Word, looks_foreign

    say = lambda text: [Word(0, 1, w) for w in text.split()]
    assert looks_foreign(say("I don't know what you want"), "pt")
    assert not looks_foreign(say("cara, olha isso aqui, que absurdo"), "pt")
    assert not looks_foreign(say("vou pegar o headshot agora, mano"), "pt")       # gíria em inglês não derruba
    assert not looks_foreign(say("the end"), "pt")                               # curto demais para julgar


def test_sensitivity_setting_is_saved_and_applies_to_running_lives(fake_record):
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})
    status = client.get("/api/live").json()
    assert status["sensitivity"] == "equilibrado" and status["sessions"][0]["per_hour"] == 4

    status = client.post("/api/live/sensitivity", json={"value": "poucos"}).json()
    assert status["sensitivity"] == "poucos" and status["sessions"][0]["per_hour"] == 2
    assert client.post("/api/live/sensitivity", json={"value": "tudo"}).status_code == 422
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_b", "auto": True})
    assert [s["per_hour"] for s in client.get("/api/live").json()["sessions"]] == [2, 2]    # vale para as novas também


def test_cut_from_live_memory_can_end_some_time_ago(tmp_path):
    from viceclips.ingest import live
    from viceclips.media import probe

    buf = tmp_path / "buf"
    buf.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=40", "-f", "lavfi",
                    "-i", "sine=f=440:d=40", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
                    "-f", "segment", "-segment_time", "4", "-segment_list", str(buf / "buffer.m3u8"),
                    str(buf / "seg_%03d.ts")], check=True)
    now = live.cut_last(buf, 10, tmp_path / "agora.mp4")
    earlier = live.cut_last(buf, 10, tmp_path / "antes.mp4", ago=20)
    assert 9 <= probe(str(now))["duration"] <= 11.5
    assert 9 <= probe(str(earlier))["duration"] <= 11.5          # mesmo tamanho, outro ponto da live

    def frame_number(path):      # o testsrc2 desenha um contador; compara pelo brilho médio do 1º frame
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        return raw

    assert frame_number(now) != frame_number(earlier)


def test_recorded_live_can_be_processed_without_a_clip_limit(monkeypatch):
    sent = {}
    monkeypatch.setattr(web.vod, "probe", lambda url: {"is_live": False, "duration": 7 * 3600.0})
    monkeypatch.setattr(web, "_submit", lambda kind, label, params, job_id=None: sent.update(params) or {"id": "x"})
    ok = client.post("/api/clips/vod", json={"url": "https://twitch.tv/videos/1", "unlimited": True})
    assert ok.status_code == 200 and sent["unlimited"] is True and sent["duration"] == 7 * 3600.0
    # sem a opção, o teto de 10 continua valendo
    assert client.post("/api/clips/vod", json={"url": "https://twitch.tv/videos/1", "auto": 50}).status_code == 422


def test_live_status_shows_how_long_it_has_been_followed(fake_record):
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})
    session = client.get("/api/live").json()["sessions"][0]
    assert session["running"] and session["uptime"] >= 0 and session["needed"] == 16.0   # aprendendo: valor inicial


def test_push_test_needs_a_subscribed_device(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "notify", lambda title, body, url="/#clips": sent.append(title))
    assert client.post("/api/push/test").status_code == 409            # ninguém ativou avisos ainda
    push.subscribe("https://fcm.googleapis.com/fcm/send/a", "p", "a", "https://site")
    assert client.post("/api/push/test").json() == {"devices": 1} and sent == ["Teste do ViceClips"]


def test_clips_expire_after_twenty_minutes_by_default():
    assert web.CLIP_TTL == 20 * 60


def test_login_survives_a_redeploy_and_ends_when_the_password_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "PASSWORD", "segredo")
    monkeypatch.setattr(web.time, "sleep", lambda _: None)
    browser = TestClient(web.app)
    assert browser.post("/api/login", json={"password": "segredo"}).status_code == 200
    cookie = browser.cookies.get("viceclips_session")

    # "Novo deploy": disco zerado, nada guardado no servidor. O mesmo cookie continua valendo.
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "disco_novo")
    again = TestClient(web.app, cookies={"viceclips_session": cookie})
    assert again.get("/api/jobs").status_code == 200

    forged = TestClient(web.app, cookies={"viceclips_session": cookie.split(".")[0] + "." + "0" * 64})
    assert forged.get("/api/jobs").status_code == 401                 # assinatura inventada
    old = TestClient(web.app, cookies={"viceclips_session": "1000." + web._sign(1000)})
    assert old.get("/api/jobs").status_code == 401                    # vencido
    monkeypatch.setattr(web, "PASSWORD", "outra")
    assert again.get("/api/jobs").status_code == 401                  # trocar a senha encerra as sessões


def test_session_cookie_lasts_a_year_and_works_when_opening_from_a_link(monkeypatch):
    monkeypatch.setattr(web, "PASSWORD", "segredo")
    header = TestClient(web.app).post("/api/login", json={"password": "segredo"}).headers["set-cookie"].lower()
    assert "max-age=31536000" in header and "samesite=lax" in header and "httponly" in header


def test_auto_clips_can_be_switched_off_for_one_live_without_stopping_it(fake_record):
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})
    session_id = client.get("/api/live").json()["sessions"][0]["id"]
    off = client.post(f"/api/live/{session_id}/auto", json={"on": False}).json()["sessions"][0]
    assert off["auto"] is False and off["running"] is True
    watcher = web.manager.get(session_id).watcher
    assert watcher.enabled is False and watcher.is_alive()            # segue ouvindo, só não clipa
    assert client.post(f"/api/live/{session_id}/auto", json={"on": True}).json()["sessions"][0]["auto"] is True


def test_followed_live_is_saved_and_offline_channel_only_gives_a_notice(fake_record, monkeypatch):
    monkeypatch.setattr(lives.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    # canal fora do ar: nada de erro; fica salvo e avisa
    def offline(url, directory):
        raise RuntimeError("O canal está offline. Para clipar uma live que já acabou, use o link do vídeo gravado.")
    monkeypatch.setattr(lives.live, "record", offline)
    res = client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_off", "auto": True})
    assert res.status_code == 200 and "não está ao vivo" in res.json()["notice"]
    saved = res.json()["channels"]
    assert [(c["name"], c["enabled"]) for c in saved] == [("canal_off", True)] and res.json()["sessions"] == []

    # pausar mantém o canal salvo, mas ele não é mais conferido
    probed = []
    monkeypatch.setattr(lives.vod, "probe", lambda url: probed.append(url) or {"is_live": False, "duration": None})
    paused = client.post("/api/channels/toggle", json={"url": "https://www.twitch.tv/canal_off", "on": False}).json()
    assert paused["channels"][0]["enabled"] is False
    web.manager.check_channels()
    assert probed == []

    # "Gravar agora" sem live: só o aviso
    notice = client.post("/api/channels/start", json={"url": "https://www.twitch.tv/canal_off"}).json()["notice"]
    assert notice == "canal_off não está ao vivo agora." and len(probed) == 1


def test_manually_followed_live_stays_in_the_saved_list_after_it_ends(fake_record, monkeypatch):
    monkeypatch.setattr(lives.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})
    status = client.get("/api/live").json()
    assert [(c["name"], c["enabled"], c["recording"]) for c in status["channels"]] == [("canal_a", False, True)]
    client.delete(f"/api/live/{status['sessions'][0]['id']}")
    after = client.get("/api/live").json()
    assert after["sessions"] == [] and [c["name"] for c in after["channels"]] == ["canal_a"]   # dá para reativar

    monkeypatch.setattr(lives.vod, "probe", lambda url: {"is_live": True, "duration": None})
    notice = client.post("/api/channels/start", json={"url": "https://www.twitch.tv/canal_a"}).json()
    assert "comecei a gravar" in notice["notice"] and len(notice["sessions"]) == 1


# ---------- proteção de disco ----------

def _disk(monkeypatch, free_gb: float):
    usage = type("U", (), {"total": 100 * 1024 ** 3, "free": int(free_gb * 1024 ** 3), "used": 0})()
    monkeypatch.setattr(lives.shutil, "disk_usage", lambda path: usage)


def test_low_disk_blocks_new_recordings_but_keeps_running_ones(fake_record, monkeypatch):
    monkeypatch.setattr(web, "_disk", "ok")
    monkeypatch.setattr(web.push, "notify", lambda *a, **k: None)
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})

    _disk(monkeypatch, 2.0)                               # abaixo dos 3 GB
    assert web.guard_disk() == "baixo"
    blocked = client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_b", "auto": True})
    assert blocked.status_code == 409 and "Pouco espaço em disco" in blocked.json()["detail"]
    status = client.get("/api/live").json()
    assert [s["running"] for s in status["sessions"]] == [True]            # a que já rodava continua
    assert "bloqueadas" in " ".join(client.get("/api/health").json()["warnings"])


def test_critical_disk_pauses_recordings_and_recovery_is_announced(fake_record, monkeypatch):
    sent = []
    monkeypatch.setattr(web, "_disk", "ok")
    monkeypatch.setattr(web.push, "notify", lambda title, body, url="/#clips": sent.append(title))
    client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_a", "auto": True})

    _disk(monkeypatch, 0.5)                               # abaixo de 1 GB
    assert web.guard_disk() == "critico"
    assert [s["running"] for s in client.get("/api/live").json()["sessions"]] == [False]
    assert sent == ["ViceClips: disco quase cheio"]
    web.guard_disk()
    assert len(sent) == 1                                 # não repete o aviso a cada conferência
    assert [c["name"] for c in client.get("/api/live").json()["channels"]] == ["canal_a"]   # canal segue salvo

    _disk(monkeypatch, 40.0)
    assert web.guard_disk() == "ok"
    assert client.post("/api/live/start", json={"url": "https://www.twitch.tv/canal_b", "auto": True}).status_code == 200
