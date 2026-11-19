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
    assert client.get("/api/ratings").json() == {"good": 1, "bad": 1}
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
    session.proc.returncode = 0
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
