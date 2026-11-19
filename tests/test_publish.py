"""Publicação: conexão das contas, agendamento e envio, contra um servidor que imita as APIs."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from viceclips import config
from viceclips.web import app as web
from viceclips.web import publish

client = TestClient(web.app, follow_redirects=False)
SITE = "http://testserver"


class FakePlatforms(BaseHTTPRequestHandler):
    """Respostas mínimas no formato documentado de cada plataforma."""
    calls: list = []

    def log_message(self, *args):
        pass

    def _reply(self, payload: dict, status: int = 200, headers: dict | None = None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()} \
            if "urlencoded" in self.headers.get("Content-Type", "") else {}
        type(self).calls.append({"method": self.command, "path": url.path, "query": query, "form": form,
                                 "size": len(raw), "headers": dict(self.headers), "raw": raw})
        host = f"http://{self.headers['Host']}"
        path = url.path
        if path == "/google/token":
            if form.get("grant_type") == "authorization_code":
                return self._reply({"access_token": "yt-access", "refresh_token": "yt-refresh", "expires_in": 3600})
            return self._reply({"access_token": "yt-access-2", "expires_in": 3600})
        if path == "/youtube/upload" and self.command == "POST":
            return self._reply({}, headers={"Location": f"{host}/youtube/session"})
        if path == "/youtube/session":
            return self._reply({"id": "VIDEO123"})
        if path == "/v2/oauth/token/":
            return self._reply({"access_token": "tt-access", "refresh_token": "tt-refresh",
                                "expires_in": 86400, "open_id": "open-abc"})
        if path == "/v2/post/publish/creator_info/query/":
            return self._reply({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})
        if path == "/v2/post/publish/video/init/":
            return self._reply({"data": {"publish_id": "p1", "upload_url": f"{host}/tiktok/upload"}})
        if path == "/tiktok/upload":
            return self._reply({}, status=201)
        if path == "/ig/oauth/access_token":
            return self._reply({"access_token": "ig-short", "user_id": 777})
        if path == "/ig/access_token":
            return self._reply({"access_token": "ig-long", "expires_in": 5184000})
        if path == "/ig/777/media":
            return self._reply({"id": "container1"})
        if path == "/ig/container1":
            return self._reply({"status_code": "FINISHED"})
        if path == "/ig/777/media_publish":
            return self._reply({"id": "media1"})
        return self._reply({"error": {"message": "rota desconhecida"}}, status=404)

    do_GET = do_POST = do_PUT = _handle


@pytest.fixture
def platforms(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatforms)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    FakePlatforms.calls = []
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(web, "PASSWORD", "")
    monkeypatch.setattr(publish, "GOOGLE_TOKEN", base + "/google/token")
    monkeypatch.setattr(publish, "YOUTUBE_UPLOAD", base + "/youtube/upload")
    monkeypatch.setattr(publish, "TIKTOK_API", base)
    monkeypatch.setattr(publish, "INSTAGRAM_TOKEN", base + "/ig/oauth/access_token")
    monkeypatch.setattr(publish, "INSTAGRAM_GRAPH", base + "/ig")
    monkeypatch.setattr(publish, "POLL_SECONDS", 0)
    for name in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
                 "INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"):
        monkeypatch.setenv(name, "chave-" + name.lower())
    config.OUT_DIR.mkdir(parents=True)
    (config.OUT_DIR / "clip_a.mp4").write_bytes(b"v" * 5000)
    (config.OUT_DIR / "clip_a.jpg").write_bytes(b"j")
    (config.OUT_DIR / "clip_a.json").write_text(json.dumps(
        {"name": "clip_a", "title": "Fuga épica", "created": time.time() - 5000, "duration": 20, "rating": None}))
    yield FakePlatforms
    server.shutdown()


def connect(platform: str) -> None:
    url = client.get(f"/api/publish/{platform}/connect").json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    back = client.get(f"/api/publish/{platform}/callback", params={"state": state, "code": "codigo"})
    assert back.status_code == 307 and "conectado" in back.headers["location"]


def test_platform_needs_keys_before_connecting(platforms, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY")
    status = {p["platform"]: p for p in client.get("/api/publish").json()["platforms"]}
    assert status["tiktok"]["configured"] is False and status["youtube"]["configured"] is True
    assert status["youtube"]["redirect"] == f"{SITE}/api/publish/youtube/callback"
    refused = client.get("/api/publish/tiktok/connect")
    assert refused.status_code == 409 and "TIKTOK_CLIENT_KEY" in refused.json()["detail"]


def test_connect_sends_user_to_the_platform_and_rejects_forged_callbacks(platforms):
    url = urlparse(client.get("/api/publish/youtube/connect").json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com" and query["redirect_uri"] == [f"{SITE}/api/publish/youtube/callback"]
    assert query["access_type"] == ["offline"] and "youtube.upload" in query["scope"][0]

    forged = client.get("/api/publish/youtube/callback", params={"state": "inventado", "code": "x"})
    assert "inv%C3%A1lido" in forged.headers["location"]
    wrong_platform = client.get("/api/publish/tiktok/callback", params={"state": query["state"][0], "code": "x"})
    assert "inv%C3%A1lido" in wrong_platform.headers["location"]
    assert publish.accounts() == {}                      # nada foi conectado
    assert platforms.calls == []                         # e nenhuma chave saiu do servidor


def test_publish_now_to_all_three_platforms(platforms):
    for platform in ("youtube", "tiktok", "instagram"):
        connect(platform)
    assert all(p["connected"] for p in client.get("/api/publish").json()["platforms"])
    platforms.calls.clear()

    res = client.post("/api/posts", json={"clip": "clip_a", "platforms": ["youtube", "tiktok", "instagram"],
                                          "caption": "Fuga épica\n#gta6"})
    assert res.status_code == 200
    deadline = time.time() + 20                       # "publicar agora" roda em segundo plano
    while time.time() < deadline:
        targets = client.get("/api/publish").json()["posts"][0]["targets"]
        if all(t["status"] != "agendado" for t in targets.values()):
            break
        time.sleep(0.1)
    assert targets["youtube"] == {"status": "publicado", "detail": "https://youtube.com/shorts/VIDEO123", "at": targets["youtube"]["at"]}
    assert targets["tiktok"]["status"] == "publicado" and "privado" in targets["tiktok"]["detail"]
    assert targets["instagram"]["status"] == "publicado"

    by_path = {c["path"]: c for c in platforms.calls}
    meta = json.loads(by_path["/youtube/upload"]["raw"])
    assert meta["snippet"]["title"] == "Fuga épica" and meta["status"]["privacyStatus"] == "public"
    assert by_path["/youtube/session"]["size"] == 5000 and by_path["/youtube/session"]["headers"]["Authorization"] == "Bearer yt-access"
    init = json.loads(by_path["/v2/post/publish/video/init/"]["raw"])
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"          # só o que a conta permite
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 5000, "chunk_size": 5000, "total_chunk_count": 1}
    assert by_path["/tiktok/upload"]["headers"]["Content-Range"] == "bytes 0-4999/5000"
    media = by_path["/ig/777/media"]["form"]
    assert media["media_type"] == "REELS" and media["access_token"] == "ig-long"

    # O Instagram baixa o vídeo por um link temporário assinado, sem precisar de login.
    link = urlparse(media["video_url"])
    assert client.get(link.path).content == b"v" * 5000
    parts = link.path.split("/")
    assert client.get("/".join(parts[:3] + ["0" * 40] + parts[4:])).status_code == 404     # assinatura errada
    assert client.get(link.path.replace(parts[2], "1000")).status_code == 404               # link vencido


def test_scheduled_post_waits_keeps_the_clip_and_then_publishes(platforms):
    connect("youtube")
    platforms.calls.clear()
    later = time.time() + 3600
    client.post("/api/posts", json={"clip": "clip_a", "platforms": ["youtube"], "caption": "x", "when": later})
    publish.run_due()
    assert platforms.calls == []                                   # ainda não é a hora
    web.sweep()
    assert (config.OUT_DIR / "clip_a.mp4").exists()                # clip velho, mas com post pendente: fica

    publish.run_due(now=later + 1)
    assert publish.posts()[0]["targets"]["youtube"]["status"] == "publicado"
    web.sweep()
    assert not (config.OUT_DIR / "clip_a.mp4").exists()            # publicado: volta a valer o prazo


def test_expired_token_is_refreshed_and_failures_are_reported(platforms):
    connect("youtube")
    data = publish.accounts()
    data["youtube"]["expires"] = time.time() - 10
    publish._save_accounts(data)
    platforms.calls.clear()
    assert publish._youtube(config.OUT_DIR / "clip_a.mp4", "t").endswith("VIDEO123")
    assert platforms.calls[0]["form"]["grant_type"] == "refresh_token"
    assert publish.accounts()["youtube"]["access"] == "yt-access-2"

    publish.schedule("clip_sumiu", None, ["youtube"], "x", None)
    publish.run_due()
    target = publish.posts()[-1]["targets"]["youtube"]
    assert target["status"] == "erro" and "não existe mais" in target["detail"]


def test_cannot_post_to_unconnected_platform_or_missing_clip(platforms):
    refused = client.post("/api/posts", json={"clip": "clip_a", "platforms": ["tiktok"], "caption": "x"})
    assert refused.status_code == 409 and "Conecte" in refused.json()["detail"]
    assert client.post("/api/posts", json={"clip": "clip_nao", "platforms": ["tiktok"]}).status_code == 404
    connect("tiktok")
    far = client.post("/api/posts", json={"clip": "clip_a", "platforms": ["tiktok"], "when": time.time() + 90 * 86400})
    assert far.status_code == 422


def test_callback_and_public_link_work_without_login_but_the_rest_does_not(platforms, monkeypatch):
    monkeypatch.setattr(web, "PASSWORD", "segredo")
    fresh = TestClient(web.app, follow_redirects=False)
    assert fresh.get("/api/publish").status_code == 401
    assert fresh.post("/api/posts", json={"clip": "clip_a", "platforms": ["youtube"]}).status_code == 401
    assert fresh.get("/api/publish/youtube/connect").status_code == 401
    assert fresh.get("/api/publish/youtube/callback", params={"state": "x", "code": "y"}).status_code == 307
    assert fresh.get(f"/public/{int(time.time()) + 60}/{'0' * 40}/clip_a.mp4").status_code == 404
