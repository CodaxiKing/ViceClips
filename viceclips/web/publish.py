"""Publicação direta e agendada em YouTube Shorts, TikTok e Instagram Reels.

Cada plataforma exige um "app de desenvolvedor" seu (as chaves entram por variáveis
de ambiente) e uma conexão da conta por OAuth, feita uma vez na tela Publicar.
Os tokens ficam em WORK_DIR/accounts.json; os posts, em WORK_DIR/posts.json.

Variáveis: YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET, TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET,
INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .. import config

# Endereços das APIs (em variáveis para os testes apontarem para um servidor falso).
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_API = "https://open.tiktokapis.com"
INSTAGRAM_AUTH = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH = "https://graph.instagram.com"

PLATFORMS = {
    "youtube": {"label": "YouTube Shorts", "id": "YOUTUBE_CLIENT_ID", "secret": "YOUTUBE_CLIENT_SECRET"},
    "tiktok": {"label": "TikTok", "id": "TIKTOK_CLIENT_KEY", "secret": "TIKTOK_CLIENT_SECRET"},
    "instagram": {"label": "Instagram Reels", "id": "INSTAGRAM_APP_ID", "secret": "INSTAGRAM_APP_SECRET"},
}
TIKTOK_CHUNK = 10 * 1024 * 1024
_lock = threading.RLock()
_states: dict[str, tuple[str, str, float]] = {}    # state -> (plataforma, endereço do site, hora)


class PublishError(Exception):
    """Erro com mensagem pronta para mostrar ao usuário."""


# ---------- HTTP ----------

def _http(method: str, url: str, *, params: dict | None = None, form: dict | None = None,
          body: dict | bytes | None = None, headers: dict | None = None, timeout: int = 120):
    """Devolve (status, cabeçalhos, corpo já decodificado de JSON quando for JSON)."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers, data = dict(headers or {}), None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(body, bytes):
        data = body
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, reply_headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, reply_headers, raw = exc.code, exc.headers, exc.read()
    except OSError as exc:
        raise PublishError(f"Sem conexão com a plataforma ({exc}).")
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"raw": raw[:300].decode("utf-8", "replace")}
    return status, reply_headers, parsed


def _fail(platform: str, status: int, reply) -> PublishError:
    detail = reply
    if isinstance(reply, dict):
        error = reply.get("error")
        detail = (error.get("message") if isinstance(error, dict) else None) or reply.get("error_description") \
            or reply.get("error_message") or error or reply.get("raw") or reply
    return PublishError(f"{PLATFORMS[platform]['label']} recusou ({status}): {str(detail)[:300]}")


def _write(path: Path, data) -> None:
    """Grava de uma vez só: quem lê nunca pega o arquivo pela metade."""
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(path)


# ---------- contas ----------

def _accounts_file() -> Path:
    return config.WORK_DIR / "accounts.json"


def accounts() -> dict:
    try:
        return json.loads(_accounts_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_accounts(data: dict) -> None:
    _write(_accounts_file(), data)


def _keys(platform: str) -> tuple[str, str]:
    spec = PLATFORMS[platform]
    return os.environ.get(spec["id"], ""), os.environ.get(spec["secret"], "")


def status() -> list[dict]:
    saved = accounts()
    return [{"platform": name, "label": spec["label"], "configured": all(_keys(name)),
             "connected": name in saved, "account": saved.get(name, {}).get("name"),
             "env": [spec["id"], spec["secret"]]}
            for name, spec in PLATFORMS.items()]


def disconnect(platform: str) -> None:
    with _lock:
        data = accounts()
        data.pop(platform, None)
        _save_accounts(data)


def _redirect(base: str, platform: str) -> str:
    return f"{base}/api/publish/{platform}/callback"


def connect_url(platform: str, base: str) -> str:
    """Endereço para onde mandar o usuário autorizar a conta."""
    client_id, client_secret = _keys(platform)
    if not (client_id and client_secret):
        spec = PLATFORMS[platform]
        raise PublishError(f"Defina {spec['id']} e {spec['secret']} no servidor antes de conectar.")
    state = secrets.token_urlsafe(24)
    with _lock:
        for old in [s for s, (_, _, at) in _states.items() if time.time() - at > 900]:
            del _states[old]
        _states[state] = (platform, base, time.time())
    redirect = _redirect(base, platform)
    if platform == "youtube":
        query = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                 "scope": "https://www.googleapis.com/auth/youtube.upload", "access_type": "offline",
                 "prompt": "consent", "state": state}
        return GOOGLE_AUTH + "?" + urllib.parse.urlencode(query)
    if platform == "tiktok":
        query = {"client_key": client_id, "redirect_uri": redirect, "response_type": "code",
                 "scope": "user.info.basic,video.publish", "state": state}
        return TIKTOK_AUTH + "?" + urllib.parse.urlencode(query)
    query = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
             "scope": "instagram_business_basic,instagram_business_content_publish", "state": state}
    return INSTAGRAM_AUTH + "?" + urllib.parse.urlencode(query)


def finish_connect(platform: str, state: str, code: str) -> None:
    """Troca o código devolvido pela plataforma pelos tokens da conta."""
    with _lock:
        expected = _states.pop(state, None)
    if not expected or expected[0] != platform:
        raise PublishError("Pedido de conexão expirado ou inválido. Tente conectar de novo.")
    base = expected[1]
    client_id, client_secret = _keys(platform)
    redirect, now = _redirect(base, platform), time.time()

    if platform == "youtube":
        code_status, _, reply = _http("POST", GOOGLE_TOKEN, form={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect, "grant_type": "authorization_code"})
        if code_status != 200 or "refresh_token" not in reply:
            raise _fail(platform, code_status, reply)
        account = {"name": "conta Google", "access": reply["access_token"],
                   "refresh": reply["refresh_token"], "expires": now + reply.get("expires_in", 3600)}
    elif platform == "tiktok":
        code_status, _, reply = _http("POST", TIKTOK_API + "/v2/oauth/token/", form={
            "client_key": client_id, "client_secret": client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect})
        if code_status != 200 or "access_token" not in reply:
            raise _fail(platform, code_status, reply)
        account = {"name": reply.get("open_id", "conta TikTok")[:12], "access": reply["access_token"],
                   "refresh": reply.get("refresh_token"), "expires": now + reply.get("expires_in", 86400)}
    else:
        code_status, _, reply = _http("POST", INSTAGRAM_TOKEN, form={
            "client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code",
            "redirect_uri": redirect, "code": code})
        if code_status != 200 or "access_token" not in reply:
            raise _fail(platform, code_status, reply)
        user_id = str(reply.get("user_id", ""))
        # Token curto (1 h) -> token longo (60 dias).
        code_status, _, long = _http("GET", INSTAGRAM_GRAPH + "/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": client_secret,
            "access_token": reply["access_token"]})
        if code_status != 200 or "access_token" not in long:
            raise _fail(platform, code_status, long)
        account = {"name": f"conta {user_id}", "user_id": user_id, "access": long["access_token"],
                   "refresh": None, "expires": now + long.get("expires_in", 60 * 86400)}

    account["base"] = base
    with _lock:
        data = accounts()
        data[platform] = account
        _save_accounts(data)


def _token(platform: str) -> dict:
    """Conta com token válido, renovando quando está para vencer."""
    with _lock:
        data = accounts()
        account = data.get(platform)
        if not account:
            raise PublishError(f"Conecte a conta de {PLATFORMS[platform]['label']} na tela Publicar.")
        client_id, client_secret = _keys(platform)
        now = time.time()
        if platform == "instagram":
            if account["expires"] - now < 7 * 86400:        # renova o token longo perto do fim
                code, _, reply = _http("GET", INSTAGRAM_GRAPH + "/refresh_access_token", params={
                    "grant_type": "ig_refresh_token", "access_token": account["access"]})
                if code == 200 and "access_token" in reply:
                    account.update(access=reply["access_token"], expires=now + reply.get("expires_in", 60 * 86400))
                    _save_accounts(data)
            return account
        if account["expires"] - now > 120:
            return account
        if platform == "youtube":
            code, _, reply = _http("POST", GOOGLE_TOKEN, form={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": account["refresh"], "grant_type": "refresh_token"})
        else:
            code, _, reply = _http("POST", TIKTOK_API + "/v2/oauth/token/", form={
                "client_key": client_id, "client_secret": client_secret,
                "refresh_token": account["refresh"], "grant_type": "refresh_token"})
        if code != 200 or "access_token" not in reply:
            raise PublishError(f"A conexão com {PLATFORMS[platform]['label']} venceu. Conecte a conta de novo.")
        account.update(access=reply["access_token"], expires=now + reply.get("expires_in", 3600))
        if reply.get("refresh_token"):
            account["refresh"] = reply["refresh_token"]
        _save_accounts(data)
        return account


# ---------- link público temporário (o Instagram baixa o vídeo do nosso servidor) ----------

def _secret() -> bytes:
    path = config.WORK_DIR / "public.key"
    try:
        return path.read_bytes()
    except OSError:
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
        return path.read_bytes()


def public_token(name: str, expires: int) -> str:
    return hmac.new(_secret(), f"{name}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]


def public_url(base: str, name: str, ttl: int = 3600) -> str:
    expires = int(time.time()) + ttl
    return f"{base}/public/{expires}/{public_token(name, expires)}/{name}.mp4"


def valid_public(name: str, expires: int, token: str) -> bool:
    return expires > time.time() and hmac.compare_digest(token, public_token(name, expires))


# ---------- envio para cada plataforma ----------

def _youtube(video: Path, caption: str) -> str:
    account = _token("youtube")
    title = (caption.splitlines()[0] if caption.strip() else "Clip")[:95]
    meta = {"snippet": {"title": title, "description": caption + "\n\n#Shorts", "categoryId": "20"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    auth = {"Authorization": f"Bearer {account['access']}"}
    code, headers, reply = _http("POST", YOUTUBE_UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                                 body=meta, headers={**auth, "X-Upload-Content-Type": "video/mp4",
                                                     "X-Upload-Content-Length": str(video.stat().st_size)})
    if code != 200 or not headers.get("Location"):
        raise _fail("youtube", code, reply)
    code, _, reply = _http("PUT", headers["Location"], body=video.read_bytes(),
                           headers={**auth, "Content-Type": "video/mp4"}, timeout=600)
    if code not in (200, 201) or "id" not in reply:
        raise _fail("youtube", code, reply)
    return f"https://youtube.com/shorts/{reply['id']}"


def _tiktok(video: Path, caption: str) -> str:
    account = _token("tiktok")
    auth = {"Authorization": f"Bearer {account['access']}"}
    code, _, info = _http("POST", TIKTOK_API + "/v2/post/publish/creator_info/query/", body={}, headers=auth)
    if code != 200:
        raise _fail("tiktok", code, info)
    options = info.get("data", {}).get("privacy_level_options") or ["SELF_ONLY"]
    # Apps ainda não auditados pelo TikTok só podem postar como privado.
    privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
    size = video.stat().st_size
    chunk = size if size <= TIKTOK_CHUNK * 2 else TIKTOK_CHUNK
    count = max(1, size // chunk)          # o último pedaço leva a sobra
    code, _, reply = _http("POST", TIKTOK_API + "/v2/post/publish/video/init/", headers=auth, body={
        "post_info": {"title": caption[:2000], "privacy_level": privacy, "disable_comment": False,
                      "disable_duet": False, "disable_stitch": False},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": count}})
    upload_url = reply.get("data", {}).get("upload_url")
    if code != 200 or not upload_url:
        raise _fail("tiktok", code, reply)
    with video.open("rb") as fh:
        for i in range(count):
            start = i * chunk
            data = fh.read(chunk if i < count - 1 else size - start)
            code, _, sent = _http("PUT", upload_url, body=data, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}"})
            if code not in (200, 201, 206):
                raise _fail("tiktok", code, sent)
    return "privado no TikTok (app ainda não auditado)" if privacy == "SELF_ONLY" else "publicado no TikTok"


def _instagram(video: Path, caption: str) -> str:
    account = _token("instagram")
    base, user, token = account["base"], account["user_id"], account["access"]
    code, _, reply = _http("POST", f"{INSTAGRAM_GRAPH}/{user}/media", form={
        "media_type": "REELS", "video_url": public_url(base, video.stem), "caption": caption[:2200],
        "access_token": token})
    if code != 200 or "id" not in reply:
        raise _fail("instagram", code, reply)
    container = reply["id"]
    for _ in range(60):                     # o Instagram baixa e processa o vídeo
        code, _, state = _http("GET", f"{INSTAGRAM_GRAPH}/{container}",
                               params={"fields": "status_code", "access_token": token})
        if state.get("status_code") == "FINISHED":
            break
        if state.get("status_code") in ("ERROR", "EXPIRED") or code != 200:
            raise _fail("instagram", code, state)
        time.sleep(POLL_SECONDS)
    else:
        raise PublishError("O Instagram demorou demais para processar o vídeo.")
    code, _, reply = _http("POST", f"{INSTAGRAM_GRAPH}/{user}/media_publish",
                           form={"creation_id": container, "access_token": token})
    if code != 200 or "id" not in reply:
        raise _fail("instagram", code, reply)
    return "publicado no Instagram"


POLL_SECONDS = 5
SENDERS = {"youtube": _youtube, "tiktok": _tiktok, "instagram": _instagram}


# ---------- posts e agendamento ----------

def _posts_file() -> Path:
    return config.WORK_DIR / "posts.json"


def posts() -> list[dict]:
    try:
        return json.loads(_posts_file().read_text())
    except (OSError, ValueError):
        return []


def _save_posts(items: list[dict]) -> None:
    _write(_posts_file(), items[-200:])


def schedule(clip: str, title: str | None, platforms: list[str], caption: str, when: float | None) -> dict:
    connected = accounts()
    for platform in platforms:
        if platform not in PLATFORMS:
            raise PublishError("Plataforma desconhecida.")
        if platform not in connected:
            raise PublishError(f"Conecte a conta de {PLATFORMS[platform]['label']} antes de publicar.")
    if not platforms:
        raise PublishError("Escolha pelo menos uma plataforma.")
    post = {"id": uuid.uuid4().hex[:10], "clip": clip, "title": title, "caption": caption.strip(),
            "when": when or time.time(), "created": time.time(),
            "targets": {p: {"status": "agendado", "detail": None} for p in platforms}}
    with _lock:
        _save_posts(posts() + [post])
    return post


def cancel(post_id: str) -> None:
    with _lock:
        _save_posts([p for p in posts() if p["id"] != post_id])


def pending_clips() -> set[str]:
    """Clips com publicação ainda por fazer: não podem ser apagados pela faxina."""
    return {p["clip"] for p in posts() if any(t["status"] == "agendado" for t in p["targets"].values())}


def run_due(now: float | None = None) -> None:
    """Publica o que já chegou na hora. Chamado periodicamente pelo servidor."""
    now = now or time.time()
    for post in posts():
        if post["when"] > now:
            continue
        for platform, target in post["targets"].items():
            if target["status"] != "agendado":
                continue
            video = config.OUT_DIR / f"{post['clip']}.mp4"
            try:
                if not video.exists():
                    raise PublishError("O clip não existe mais no servidor.")
                detail, state = SENDERS[platform](video, post["caption"]), "publicado"
            except PublishError as exc:
                detail, state = str(exc), "erro"
            except Exception as exc:      # um erro inesperado não pode travar os outros posts
                detail, state = f"Erro inesperado: {exc}", "erro"
            with _lock:
                items = posts()
                for item in items:
                    if item["id"] == post["id"]:
                        item["targets"][platform] = {"status": state, "detail": detail, "at": time.time()}
                _save_posts(items)
