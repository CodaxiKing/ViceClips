"""Interface web do ViceClips: API + página única servida em /.

Um worker processa os jobs em fila (um render por vez). A fila é gravada em
WORK_DIR/jobs.json e retomada quando o servidor reinicia; os clips prontos ficam em
OUT_DIR com um .json (metadados) e um .jpg (capa).

Com VICECLIPS_PASSWORD definida, tudo exige login (cookie de sessão).
"""
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..config import Rect
from ..highlights.chat import ChatMeter, TwitchChat, twitch_channel
from ..highlights.live_watch import LiveWatcher
from ..ingest import live, vod
from ..media import frame_jpeg, parse_time, probe
from ..pipeline import ai_titles_enabled, build_clip

STATIC = Path(__file__).parent / "static"
MAX_CLIP_SECONDS = 180
CLIP_NAME = re.compile(r"^clip_[0-9a-z_]+$")

app = FastAPI(title="ViceClips")
_executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_live: dict = {"proc": None, "url": None, "started": None, "watcher": None}

PASSWORD = os.environ.get("VICECLIPS_PASSWORD", "")
SESSION_COOKIE = "viceclips_session"
MAX_JOBS = 50
_state_lock = threading.Lock()
_login_lock = threading.Lock()


def _jobs_file() -> Path:
    return config.WORK_DIR / "jobs.json"


def _sessions_file() -> Path:
    return config.WORK_DIR / "sessions.json"


# ---------- login ----------

def _sessions() -> list[str]:
    try:
        return json.loads(_sessions_file().read_text())
    except (OSError, ValueError):
        return []


def _authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token) and any(hmac.compare_digest(token, t) for t in _sessions())


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    public = path in ("/", "/sw.js", "/api/login", "/api/session") or path.startswith("/static/")
    if not public and not _authed(request):
        return JSONResponse({"detail": "Faça login."}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(req: Login, request: Request, response: Response) -> dict:
    with _login_lock:  # uma tentativa por vez, com atraso: freia força bruta
        if not PASSWORD or not hmac.compare_digest(req.password.encode(), PASSWORD.encode()):
            time.sleep(1)
            raise HTTPException(401, "Senha incorreta.")
        token = secrets.token_urlsafe(32)
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        _sessions_file().write_text(json.dumps((_sessions() + [token])[-20:]))
    response.set_cookie(SESSION_COOKIE, token, max_age=30 * 86400, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    with _login_lock:
        _sessions_file().write_text(json.dumps([t for t in _sessions() if t != token]))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/session")
def session(request: Request) -> dict:
    return {"auth_required": bool(PASSWORD), "authed": _authed(request),
            "ai_titles": ai_titles_enabled()}


# ---------- jobs ----------

def _save_jobs() -> None:
    with _state_lock:
        with _lock:
            jobs = sorted(_jobs.values(), key=lambda j: j["created"])[-MAX_JOBS:]
            for old in set(_jobs) - {j["id"] for j in jobs}:
                del _jobs[old]
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _jobs_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs))
        tmp.replace(_jobs_file())


def _submit(kind: str, label: str, params: dict, job_id: str | None = None) -> dict:
    """Enfileira um job. `params` precisa ser JSON puro: é o que permite retomá-lo."""
    job = {"id": job_id or uuid.uuid4().hex[:10], "kind": kind, "label": label,
           "params": params, "status": "fila", "step": "Na fila", "error": None,
           "clips": [], "created": time.time()}
    with _lock:
        _jobs[job["id"]] = job
    _save_jobs()
    _executor.submit(_run, job)
    return _public(job)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "params"}


def _run(job: dict) -> None:
    job["status"] = "processando"
    _save_jobs()
    try:
        WORKERS[job["kind"]](job)
        job["status"], job["step"] = "pronto", "Pronto"
    except Exception as exc:
        job["status"], job["step"] = "erro", "Erro"
        job["error"] = str(exc)[-600:]
    _save_jobs()


def _resume_jobs() -> None:
    """Recarrega a fila do disco e reenfileira o que não tinha terminado."""
    try:
        saved = json.loads(_jobs_file().read_text())
    except (OSError, ValueError):
        return
    for job in saved:
        _jobs[job["id"]] = job
        if job["status"] in ("fila", "processando"):
            job.update(status="fila", step="Na fila (retomado)", clips=[])
            _executor.submit(_run, job)


def _render(job: dict, src: Path, name: str, opts: dict, source: str) -> None:
    """Renderiza `src` em OUT_DIR/name.mp4 e grava capa e metadados."""
    def step(text: str) -> None:
        job["step"] = text

    out = config.OUT_DIR / f"{name}.mp4"
    cam = Rect.parse(opts["cam"]) if opts.get("cam") else None
    path, title, _ = build_clip(str(src), out, title=opts.get("title"), cam=cam,
                                credit=opts.get("credit"), log=step)
    duration = probe(path)["duration"]
    out.with_suffix(".jpg").write_bytes(frame_jpeg(path, duration / 2, width=360))
    out.with_suffix(".json").write_text(json.dumps({
        "name": name, "title": title, "credit": opts.get("credit"), "source": source,
        "kind": opts.get("kind"), "score": opts.get("score"),
        "duration": round(duration, 1), "created": time.time()}))
    job["clips"].append(name)


def _options(title: str | None, credit: str | None, cam: str | None) -> dict:
    cam = (cam or "").strip() or None
    try:
        if cam:
            Rect.parse(cam)
    except (ValueError, TypeError):
        raise HTTPException(422, "Câmera deve ser X,Y,L,A (quatro números).")
    return {"title": (title or "").strip() or None, "credit": (credit or "").strip() or None,
            "cam": cam}


def _check_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://\S+$", url):
        raise HTTPException(422, "Informe um link http(s) válido.")
    return url


# ---------- API ----------

class VodRequest(BaseModel):
    url: str
    start: str | None = None
    end: str | None = None
    auto: int | None = None
    title: str | None = None
    credit: str | None = None
    cam: str | None = None


@app.post("/api/clips/vod")
def create_vod(req: VodRequest) -> dict:
    url, opts = _check_url(req.url), _options(req.title, req.credit, req.cam)
    window = None
    if req.auto:
        if not 1 <= req.auto <= 10:
            raise HTTPException(422, "Quantidade automática deve ficar entre 1 e 10.")
    else:
        try:
            window = (parse_time(req.start or ""), parse_time(req.end or ""))
        except ValueError:
            raise HTTPException(422, "Informe início e fim, ex.: 1:23:45.")
        if not 0 < window[1] - window[0] <= MAX_CLIP_SECONDS:
            raise HTTPException(422, f"O trecho deve ter entre 1 e {MAX_CLIP_SECONDS} segundos.")

    try:
        info = vod.probe(url)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    if info["is_live"]:
        # Link de live que está no ar: não há vídeo pronto para baixar; acompanha ao vivo.
        _start_live(url, auto=True, credit=opts["credit"])
        return {"live": True}
    if window and info["duration"] and window[0] >= info["duration"]:
        raise HTTPException(422, "O início informado passa do fim do vídeo.")

    return _submit("gravada", url, {"url": url, "window": window, "auto": req.auto, "opts": opts})


def _work_vod(job: dict) -> None:
    p, stamp = job["params"], job["id"]
    url = p["url"]
    # Cada item: (início, fim, opções do clip). No automático as opções levam tipo e nota.
    windows = [(*p["window"], p["opts"])] if p["window"] else []
    if p["auto"]:
        from ..highlights.audio import find_highlights
        job["step"] = "Baixando áudio da live"
        audio = vod.download_audio(url, config.WORK_DIR / f"vod_{stamp}.m4a")
        job["step"] = "Analisando a live inteira"
        windows = [(m.start, m.end, {**p["opts"], "kind": m.kind, "score": m.score})
                   for m in find_highlights(str(audio), count=p["auto"])]
        audio.unlink(missing_ok=True)
        if not windows:
            raise RuntimeError("Não achei nenhum momento marcante nessa live; "
                               "tente o modo por intervalo.")
    for i, (start, end, opts) in enumerate(windows, 1):
        job["step"] = f"Baixando trecho {i}/{len(windows)}"
        raw = vod.download_section(url, start, end, config.WORK_DIR / f"vod_{stamp}_{i}.mp4")
        _render(job, raw, f"clip_{stamp}_{i}", opts, url)


def _work_file(job: dict) -> None:
    """Trecho já em disco (upload ou corte da live)."""
    p = job["params"]
    _render(job, Path(p["raw"]), f"clip_{job['id']}", p["opts"], p["source"])


WORKERS = {"gravada": _work_vod, "arquivo": _work_file, "ao vivo": _work_file}


@app.post("/api/clips/upload")
def create_upload(file: UploadFile = File(...), title: str = Form(""),
                  credit: str = Form(""), cam: str = Form("")) -> dict:
    opts = _options(title, credit, cam)
    job_id = uuid.uuid4().hex[:10]
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".ts", ".flv"}:
        raise HTTPException(422, "Envie um vídeo (.mp4, .mkv, .mov, .webm, .ts ou .flv).")
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    raw = config.WORK_DIR / f"upload_{job_id}{suffix}"
    with raw.open("wb") as fh:
        while chunk := file.file.read(1 << 20):
            fh.write(chunk)
    label = file.filename or "arquivo"
    return _submit("arquivo", label, {"raw": str(raw), "opts": opts, "source": label}, job_id)


class LiveStart(BaseModel):
    url: str
    auto: bool = True      # clipar sozinho quando o áudio disparar
    credit: str | None = None


class LiveClip(BaseModel):
    last: float = 45
    title: str | None = None
    credit: str | None = None
    cam: str | None = None


def _live_dir() -> Path:
    return config.WORK_DIR / "live"


def _live_status() -> dict:
    proc = _live["proc"]
    playlist = _live_dir() / "buffer.m3u8"
    buffered = 0.0
    if _live["url"] and playlist.exists():
        buffered = sum(d for _, d in live._buffer_segments(playlist))
    running = bool(proc and proc.poll() is None)
    # ffmpeg saiu com erro sem gravar nada: o link não é uma live que dê para ler.
    failed = bool(proc and not running and proc.returncode and buffered == 0)
    watcher, chat = _live["watcher"], _live.get("chat")
    return {"running": running, "failed": failed, "url": _live["url"],
            "chat": bool(chat and chat.connected),
            "auto": bool(watcher), "auto_clips": watcher.clips if watcher else 0,
            "started": _live["started"], "buffered": round(buffered),
            "capacity": live.SEGMENT_SECONDS * live.BUFFER_SEGMENTS}


@app.get("/api/live")
def live_status() -> dict:
    return _live_status()


@app.post("/api/live/start")
def live_start(req: LiveStart) -> dict:
    return _start_live(_check_url(req.url), req.auto, req.credit)


def _start_live(url: str, auto: bool, credit: str | None) -> dict:
    if _live_status()["running"]:
        raise HTTPException(409, "Já existe uma live sendo acompanhada; pare-a na tela Ao vivo antes.")
    for old in _live_dir().glob("*"):
        old.unlink()
    try:
        _live["proc"] = live.record(url, _live_dir())
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    _live.update(url=url, started=time.time(), watcher=None, chat=None)
    if auto:
        opts = _options(None, credit, None)
        meter, channel = None, twitch_channel(url)
        if channel:   # o chat da Twitch reforça a análise; sem ele segue só o áudio
            meter = ChatMeter()
            _live["chat"] = TwitchChat(channel, meter)
            _live["chat"].start()

        def on_highlight(seconds: float, kind: str, score: float) -> None:
            _clip_from_live(min(seconds, MAX_CLIP_SECONDS), {**opts, "kind": kind, "score": score})

        _live["watcher"] = LiveWatcher(_live_dir(), on_highlight, chat=meter)
        _live["watcher"].start()
    return _live_status()


@app.post("/api/live/stop")
def live_stop() -> dict:
    proc = _live["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)
    for worker in (_live["watcher"], _live.get("chat")):
        if worker:
            worker.stop()
    return _live_status()


def _clip_from_live(seconds: float, opts: dict) -> dict:
    """Corta os últimos `seconds` do buffer e enfileira o render."""
    job_id = uuid.uuid4().hex[:10]
    raw = live.cut_last(_live_dir(), seconds, config.WORK_DIR / f"live_{job_id}.mp4")
    source = _live["url"] or "live"
    return _submit("ao vivo", source, {"raw": str(raw), "opts": opts, "source": source}, job_id)


@app.post("/api/live/clip")
def live_clip(req: LiveClip) -> dict:
    status = _live_status()
    if not 1 <= req.last <= MAX_CLIP_SECONDS:
        raise HTTPException(422, f"Escolha entre 1 e {MAX_CLIP_SECONDS} segundos.")
    if status["buffered"] < 1:
        raise HTTPException(409, "O buffer ainda está vazio; aguarde alguns segundos.")
    # Corta já, na requisição, para pegar o momento exato do clique.
    return _clip_from_live(req.last, _options(req.title, req.credit, req.cam))


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with _lock:
        return [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)]


@app.get("/api/clips")
def list_clips() -> list[dict]:
    clips = []
    for meta in config.OUT_DIR.glob("clip_*.json"):
        if meta.with_suffix(".mp4").exists():
            clips.append(json.loads(meta.read_text()))
    return sorted(clips, key=lambda c: c["created"], reverse=True)


def _clip_file(name: str, suffix: str) -> Path:
    path = config.OUT_DIR / f"{name}{suffix}"
    if not CLIP_NAME.match(name) or not path.exists():
        raise HTTPException(404, "Clip não encontrado.")
    return path


@app.get("/media/{name}.mp4")
def clip_video(name: str, download: bool = False) -> FileResponse:
    path = _clip_file(name, ".mp4")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"{name}.mp4" if download else None)


@app.get("/media/{name}.jpg")
def clip_thumb(name: str) -> FileResponse:
    return FileResponse(_clip_file(name, ".jpg"), media_type="image/jpeg")


@app.delete("/api/clips/{name}")
def delete_clip(name: str) -> dict:
    _clip_file(name, ".mp4")
    for suffix in (".mp4", ".jpg", ".json"):
        (config.OUT_DIR / f"{name}{suffix}").unlink(missing_ok=True)
    return {"deleted": name}


@app.get("/")
def index(request: Request) -> FileResponse:
    page = "index.html" if _authed(request) else "login.html"
    return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Servido na raiz para o service worker valer para o site inteiro.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


_resume_jobs()


app.mount("/static", StaticFiles(directory=STATIC), name="static")
