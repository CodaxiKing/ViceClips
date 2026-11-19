import pytest
from fastapi.testclient import TestClient

from viceclips.web.app import app

client = TestClient(app)


def test_index_and_empty_lists():
    assert client.get("/").status_code == 200
    assert client.get("/api/live").json()["running"] is False


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


def test_clip_names_cannot_escape_out_dir():
    assert client.get("/media/..%2F..%2Fpyproject.mp4").status_code == 404
    assert client.delete("/api/clips/..%2Fsecret").status_code == 404
    assert client.get("/media/clip_naoexiste.mp4").status_code == 404
