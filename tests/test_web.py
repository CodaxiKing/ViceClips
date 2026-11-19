import pytest
from fastapi.testclient import TestClient

import json
import subprocess
import time

from viceclips import config
from viceclips.web import app as web
from viceclips.web.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(web, "PASSWORD", "")
    web._jobs.clear()


def test_index_and_empty_lists():
    assert client.get("/").status_code == 200
    assert client.get("/api/live").json()["sessions"] == []


@pytest.mark.parametrize("body", [
    {"url": "não é link", "start": "0:10", "end": "0:20"},
    {"url": "--exec=evil", "start": "0:10", "end": "0:20"},
    {"url": "https://x.tv/v/1"},
    {"url": "https://x.tv/v/1", "start": "0:20", "end": "0:10"},
    {"url": "https://x.tv/v/1", "start": "0:00", "end": "10:00"},
    {"url": "https://x.tv/v/1", "auto": 50},
    {"url": "https://x.tv/v/1", "start": "0:10", "end": "0:20", "cam": "1,2,3"},
])
def test_vod_rejects_bad_input(body):
    assert client.post("/api/clips/vod", json=body).status_code == 422


def test_no_title_without_anthropic_key(monkeypatch):
    from viceclips import pipeline

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    assert pipeline._auto_title("qualquer.mp4", []) is None
    assert client.get("/api/session").json()["ai_titles"] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert client.get("/api/session").json()["ai_titles"] is True


def test_live_link_in_new_clip_starts_watching_instead_of_downloading(monkeypatch):
    started = {}
    monkeypatch.setattr(web.vod, "probe", lambda url: {"is_live": True, "duration": None})
    monkeypatch.setattr(web, "_start_live", lambda url, auto, credit: started.update(url=url, auto=auto))
    res = client.post("/api/clips/vod", json={"url": "https://twitch.tv/canal", "auto": 5})
    assert res.json() == {"live": True}
    assert started == {"url": "https://twitch.tv/canal", "auto": True}
    assert web._jobs == {}


def test_offline_channel_gets_a_useful_message(monkeypatch):
    from viceclips.ingest.vod import friendly_error

    message = friendly_error("ERROR: [twitch:stream] canal: The channel is not currently live")
    assert "offline" in message and "twitch.tv/videos" in message

    def offline(url):
        raise RuntimeError(message)
    monkeypatch.setattr(web.vod, "probe", offline)
    res = client.post("/api/clips/vod", json={"url": "https://twitch.tv/canal", "auto": 5})
    assert res.status_code == 422 and res.json()["detail"] == message


def test_clip_names_cannot_escape_out_dir():
    assert client.get("/media/..%2F..%2Fpyproject.mp4").status_code == 404
    assert client.delete("/api/clips/..%2Fsecret").status_code == 404
    assert client.get("/media/clip_naoexiste.mp4").status_code == 404


def test_password_protects_api_and_login_opens_it(monkeypatch):
    monkeypatch.setattr(web, "PASSWORD", "segredo")
    monkeypatch.setattr(web.time, "sleep", lambda _: None)
    fresh = TestClient(app)
    assert fresh.get("/api/jobs").status_code == 401
    assert fresh.get("/media/clip_x.mp4").status_code == 401
    assert "Senha" in fresh.get("/").text
    assert fresh.post("/api/login", json={"password": "errada"}).status_code == 401
    assert fresh.get("/api/jobs").status_code == 401

    assert fresh.post("/api/login", json={"password": "segredo"}).status_code == 200
    assert fresh.get("/api/jobs").status_code == 200
    assert "Novo clip" in fresh.get("/").text

    fresh.post("/api/logout")
    assert fresh.get("/api/jobs").status_code == 401


def test_unfinished_jobs_resume_after_restart(tmp_path):
    raw = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast", str(raw)], check=True)
    opts = {"title": "Retomado", "credit": None, "cam": None}
    saved = [
        {"id": "aaa111", "kind": "arquivo", "label": "raw.mp4", "status": "processando",
         "step": "Renderizando", "error": None, "clips": [], "created": 1.0,
         "params": {"raw": str(raw), "opts": opts, "source": "raw.mp4"}},
        {"id": "bbb222", "kind": "arquivo", "label": "antigo.mp4", "status": "pronto",
         "step": "Pronto", "error": None, "clips": ["clip_bbb222"], "created": 0.5, "params": {}},
    ]
    config.WORK_DIR.mkdir(parents=True)
    web._jobs_file().write_text(json.dumps(saved))

    web._resume_jobs()

    deadline = time.time() + 30
    while web._jobs["aaa111"]["status"] != "pronto" and time.time() < deadline:
        assert web._jobs["aaa111"]["status"] != "erro", web._jobs["aaa111"]["error"]
        time.sleep(0.2)
    assert web._jobs["aaa111"]["clips"] == ["clip_aaa111"]
    assert (config.OUT_DIR / "clip_aaa111.mp4").exists()
    assert web._jobs["bbb222"]["status"] == "pronto"
    on_disk = {j["id"]: j["status"] for j in json.loads(web._jobs_file().read_text())}
    assert on_disk == {"aaa111": "pronto", "bbb222": "pronto"}
    assert "params" not in client.get("/api/jobs").json()[0]
