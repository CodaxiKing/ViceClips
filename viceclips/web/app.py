"""Interface web do ViceClips: API + página única servida em /.

Um worker processa os jobs em fila (um render por vez). A fila é gravada em
WORK_DIR/jobs.json e retomada quando o servidor reinicia. Os clips prontos ficam em
OUT_DIR com um .json (metadados) e um .jpg (capa), e são apagados sozinhos depois de
CLIP_TTL segundos.

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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..ai import transcribe
from ..config import Rect
from ..ingest import vod
from ..media import frame_jpeg, parse_time, probe
from ..pipeline import ai_titles_enabled, build_clip
from . import push
from .lives import MAX_CLIP_SECONDS, LiveError, LiveManager, LiveSession

STATIC = Path(__file__).parent / "static"
CLIP_NAME = re.compile(r"^clip_[0-9a-z_]+$")
# Minutos até um clip pronto ser apagado; 0 guarda para sempre.
CLIP_TTL = int(os.environ.get("VICECLIPS_CLIP_TTL_MINUTES", "10")) * 60
STALE_WORK_SECONDS = 2 * 3600      # sobras de processamento interrompido
PASSWORD = os.environ.get("VICECLIPS_PASSWORD", "")
SESSION_COOKIE = "viceclips_session"
MAX_JOBS = 50

_executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_state_lock = threading.Lock()
_login_lock = threading.Lock()
_halt = threading.Event()
_known_cams: dict[str, Rect] = {}     # origem (link da live) -> onde a câmera foi vista


def _queue_live_clip(session: LiveSession, raw: Path, opts: dict, job_id: str) -> dict:
    return _submit("ao vivo", session.name,
                   {"raw": str(raw), "opts": opts, "source": session.url}, job_id)


manager = LiveManager(_queue_live_clip)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _resume_jobs()
    manager.start_background()
    threading.Thread(target=_janitor, daemon=True).start()
    yield
    _halt.set()
    manager.shutdown()


app = FastAPI(title="ViceClips", lifespan=lifespan)


def _jobs_file() -> Path:
    return config.WORK_DIR / "jobs.json"


def _sessions_file() -> Path:
    return config.WORK_DIR / "sessions.json"


def _ratings_file() -> Path:
    return config.WORK_DIR / "ratings.jsonl"


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
            "ai_titles": ai_titles_enabled(), "captions": transcribe.available(),
            "clip_ttl_minutes": CLIP_TTL // 60}


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
    """Renderiza `src` em OUT_DIR/name.mp4, grava capa e metadados e avisa o celular."""
    def step(text: str) -> None:
        job["step"] = text

    out = config.OUT_DIR / f"{name}.mp4"
    cam = Rect.parse(opts["cam"]) if opts.get("cam") else None
    path, title, detected = build_clip(str(src), out, title=opts.get("title"), cam=cam,
                                       credit=opts.get("credit"), log=step,
                                       known_cam=_known_cams.get(source))
    if detected:
        _known_cams[source] = detected     # mesma live, próxima vez: câmera no mesmo lugar
    duration = probe(path)["duration"]
    out.with_suffix(".jpg").write_bytes(frame_jpeg(path, duration / 2, width=360))
    out.with_suffix(".json").write_text(json.dumps({
        "name": name, "title": title, "credit": opts.get("credit"), "source": source,
        "kind": opts.get("kind"), "score": opts.get("score"), "rating": None,
        "duration": round(duration, 1), "created": time.time()}))
    job["clips"].append(name)
    src.unlink(missing_ok=True)   # o trecho bruto não é mais necessário

    what = {"engraçado": "Momento engraçado", "impactante": "Momento impactante"}.get(opts.get("kind"), "Clip")
    minutes = f" Fica disponível por {CLIP_TTL // 60} min." if CLIP_TTL else ""
    push.notify("Clip pronto", f"{title or what} · {job['label'][:40]}.{minutes}")


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


# ---------- faxina ----------

def sweep(now: float | None = None) -> None:
    """Apaga clips vencidos e sobras antigas de processamento."""
    now = now or time.time()
    if CLIP_TTL:
        for meta in config.OUT_DIR.glob("clip_*.json"):
            try:
                expired = now - json.loads(meta.read_text())["created"] > CLIP_TTL
            except (OSError, ValueError, KeyError):
                expired = now - meta.stat().st_mtime > CLIP_TTL
            if expired:
                for suffix in (".mp4", ".jpg", ".json"):
                    meta.with_suffix(suffix).unlink(missing_ok=True)
    for pattern in ("upload_*", "live_*.mp4", "vod_*"):
        for leftover in config.WORK_DIR.glob(pattern):
            if leftover.is_file() and now - leftover.stat().st_mtime > STALE_WORK_SECONDS:
                leftover.unlink(missing_ok=True)


def _janitor() -> None:
    while not _halt.wait(20):
        try:
            sweep()
        except Exception:
            pass


# ---------- clips a partir de link ou arquivo ----------

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


# ---------- lives e canais ----------

class LiveStart(BaseModel):
    url: str
    auto: bool = True      # clipar sozinho quando o áudio disparar
    credit: str | None = None


class LiveClip(BaseModel):
    last: float = 45
    title: str | None = None
    credit: str | None = None
    cam: str | None = None


class Channel(BaseModel):
    url: str
    credit: str | None = None


def _start_live(url: str, auto: bool, credit: str | None) -> dict:
    try:
        manager.start(url, auto, (credit or "").strip() or None)
    except LiveError as exc:
        raise HTTPException(409, str(exc))
    return manager.status()


def _live_session(session_id: str) -> LiveSession:
    found = manager.get(session_id)
    if not found:
        raise HTTPException(404, "Essa live não está mais na lista.")
    return found


@app.get("/api/live")
def live_status() -> dict:
    return manager.status()


@app.post("/api/live/start")
def live_start(req: LiveStart) -> dict:
    return _start_live(_check_url(req.url), req.auto, req.credit)


@app.post("/api/live/{session_id}/stop")
def live_stop(session_id: str) -> dict:
    _live_session(session_id).stop()
    return manager.status()


@app.delete("/api/live/{session_id}")
def live_remove(session_id: str) -> dict:
    _live_session(session_id)
    manager.remove(session_id)
    return manager.status()


@app.post("/api/live/{session_id}/clip")
def live_clip(session_id: str, req: LiveClip) -> dict:
    found = _live_session(session_id)
    if not 1 <= req.last <= MAX_CLIP_SECONDS:
        raise HTTPException(422, f"Escolha entre 1 e {MAX_CLIP_SECONDS} segundos.")
    if found.buffered() < 1:
        raise HTTPException(409, "O buffer ainda está vazio; aguarde alguns segundos.")
    opts = _options(req.title, req.credit or found.credit, req.cam)
    # Corta já, na requisição, para pegar o momento exato do clique.
    return found.clip(req.last, opts)


@app.post("/api/channels")
def channel_add(req: Channel) -> dict:
    try:
        manager.add_channel(_check_url(req.url), (req.credit or "").strip() or None)
    except LiveError as exc:
        raise HTTPException(409, str(exc))
    return manager.status()


@app.post("/api/channels/remove")
def channel_remove(req: Channel) -> dict:
    manager.remove_channel(req.url)
    return manager.status()


# ---------- fila e galeria ----------

@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with _lock:
        return [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)]


@app.get("/api/clips")
def list_clips() -> list[dict]:
    clips, now = [], time.time()
    for meta in config.OUT_DIR.glob("clip_*.json"):
        if meta.with_suffix(".mp4").exists():
            clip = json.loads(meta.read_text())
            clip["expires_in"] = max(0, round(clip["created"] + CLIP_TTL - now)) if CLIP_TTL else None
            clips.append(clip)
    return sorted(clips, key=lambda c: c["created"], reverse=True)


def _clip_file(name: str, suffix: str) -> Path:
    path = config.OUT_DIR / f"{name}{suffix}"
    if not CLIP_NAME.match(name) or not path.exists():
        raise HTTPException(404, "Clip não encontrado (pode já ter sido apagado).")
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


# ---------- avaliações ----------

class Rating(BaseModel):
    good: bool


@app.post("/api/clips/{name}/rating")
def rate_clip(name: str, req: Rating) -> dict:
    """Guarda o "bom/ruim" do clip. O registro sobrevive à exclusão do vídeo."""
    meta_path = _clip_file(name, ".json")
    meta = json.loads(meta_path.read_text())
    meta["rating"] = "bom" if req.good else "ruim"
    meta_path.write_text(json.dumps(meta))
    record = {"ts": time.time(), "name": name, "good": req.good, "kind": meta.get("kind"),
              "score": meta.get("score"), "auto": meta.get("kind") is not None,
              "source": meta.get("source"), "duration": meta.get("duration")}
    with _state_lock:
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        with _ratings_file().open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    return {"rating": meta["rating"], **ratings_summary()}


@app.get("/api/ratings")
def ratings_summary() -> dict:
    latest: dict[str, dict] = {}
    try:
        for line in _ratings_file().read_text().splitlines():
            record = json.loads(line)
            latest[record["name"]] = record       # vale a última avaliação de cada clip
    except (OSError, ValueError):
        pass
    good = sum(r["good"] for r in latest.values())
    return {"good": good, "bad": len(latest) - good}


# ---------- avisos no celular ----------

class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushKeys | None = None


@app.get("/api/push/key")
def push_key() -> dict:
    return {"key": push.public_key()}


@app.post("/api/push/subscribe")
def push_subscribe(req: PushSubscription, request: Request) -> dict:
    if not req.keys or not push.allowed_endpoint(req.endpoint):
        raise HTTPException(422, "Esse navegador não é suportado para avisos.")
    # O serviço de push exige um contato do servidor; o próprio endereço do site serve.
    contact = f"{request.url.scheme}://{request.url.netloc}"
    push.subscribe(req.endpoint, req.keys.p256dh, req.keys.auth, contact)
    push.notify("Avisos ativados", "Você será avisado aqui quando um clip ficar pronto.")
    return {"ok": True}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(req: PushSubscription) -> dict:
    push.unsubscribe(req.endpoint)
    return {"ok": True}


# ---------- páginas ----------

@app.get("/")
def index(request: Request) -> FileResponse:
    page = "index.html" if _authed(request) else "login.html"
    return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Servido na raiz para o service worker valer para o site inteiro.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
