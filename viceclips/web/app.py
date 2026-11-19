"""Interface web do ViceClips: API + página única servida em /.

Um worker processa os jobs em fila (um render por vez). A fila é gravada em
WORK_DIR/jobs.json e retomada quando o servidor reinicia. Os clips prontos ficam em
OUT_DIR com um .json (metadados) e um .jpg (capa), e são apagados sozinhos depois de
CLIP_TTL segundos.

Com VICECLIPS_PASSWORD definida, tudo exige login (cookie de sessão).
"""
import hashlib
import hmac
import json
import os
import shutil
import re
import secrets
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel

from .. import config
from ..ai import transcribe
from ..config import Rect
from ..highlights import calibration
from ..ingest import vod
from ..media import frame_jpeg, parse_time, probe
from ..pipeline import NoStreamer, ai_titles_enabled, build_clip
from . import activity, publish, push
from ..highlights.rate import SENSITIVITY
from .lives import MAX_CLIP_SECONDS, LiveError, LiveManager, LiveSession, display_name, sensitivity

STATIC = Path(__file__).parent / "static"
CLIP_NAME = re.compile(r"^clip_[0-9a-z_]+$")
# Minutos até um clip pronto ser apagado; 0 guarda para sempre.
CLIP_TTL = int(os.environ.get("VICECLIPS_CLIP_TTL_MINUTES", "20")) * 60
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
_started_at = time.time()
_known_cams: dict[str, Rect] = {}     # origem (link da live) -> onde a câmera foi vista


def _queue_live_clip(session: LiveSession, raw: Path, opts: dict, job_id: str) -> dict:
    return _submit("ao vivo", session.name,
                   {"raw": str(raw), "opts": opts, "source": session.url}, job_id)


manager = LiveManager(_queue_live_clip)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _resume_jobs()
    calibration.apply(_rating_records())
    manager.start_background()
    threading.Thread(target=_janitor, daemon=True).start()
    threading.Thread(target=_publisher, daemon=True).start()
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

SESSION_DAYS = 365


def _sign(expires: int) -> str:
    """Assinatura do cookie de sessão, derivada da senha do site.

    Nada fica guardado no servidor: o login sobrevive a reinícios e a novos deploys (antes
    as sessões ficavam num arquivo e sumiam junto com ele). Trocar a senha encerra todas.
    """
    key = hashlib.sha256(b"viceclips-session:" + PASSWORD.encode()).digest()
    return hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    return f"{expires}.{_sign(expires)}"


def _authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    expires, _, signature = request.cookies.get(SESSION_COOKIE, "").partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(int(expires)))


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # Além das páginas básicas, ficam abertos: a volta do OAuth (validada pelo `state`) e o
    # link temporário assinado de onde o Instagram baixa o vídeo.
    public = (path in ("/", "/sw.js", "/api/login", "/api/session") or path.startswith("/static/")
              or path.startswith("/public/")
              or (path.startswith("/api/publish/") and path.endswith("/callback")))
    if not public and not _authed(request):
        return JSONResponse({"detail": "Faça login."}, status_code=401)
    response = await call_next(request)
    if path.startswith("/static/"):
        # Sempre confere se o arquivo mudou: depois de uma atualização, o navegador não pode
        # ficar com a página nova e o script antigo.
        response.headers["Cache-Control"] = "no-cache"
    return response


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(req: Login, request: Request, response: Response) -> dict:
    with _login_lock:  # uma tentativa por vez, com atraso: freia força bruta
        if not PASSWORD or not hmac.compare_digest(req.password.encode(), PASSWORD.encode()):
            time.sleep(1)
            raise HTTPException(401, "Senha incorreta.")
    # "lax" (e não "strict"): o cookie também vale ao abrir o site por um link, por um aviso
    # no celular ou pelo "Compartilhar" de outro app; com "strict" esses caminhos caíam no login.
    response.set_cookie(SESSION_COOKIE, _new_session(), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
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
        ahead = sum(j["status"] in ("fila", "processando") for j in _jobs.values()) - 1
    _save_jobs()
    activity.log("Entrou na fila" + (f" ({ahead} na frente)" if ahead else ""), "info", label)
    _executor.submit(_run, job)
    return _public(job)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "params"}


def _run(job: dict) -> None:
    job["status"], job["started"] = "processando", time.time()
    _save_jobs()
    activity.log("Começou a processar", "info", job["label"])
    try:
        WORKERS[job["kind"]](job)
        job["status"], job["step"] = "pronto", "Pronto"
        if not job["clips"]:
            activity.log("Terminou sem gerar clip (o trecho não mostrava o streamer na câmera)", "warn", job["label"])
    except Exception as exc:
        job["status"], job["step"] = "erro", "Erro"
        job["error"] = str(exc)[-600:]
        activity.log(f"Falhou: {job['error']}", "error", job["label"])
    job["finished"] = time.time()
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


def _render(job: dict, src: Path, name: str, opts: dict, source: str, words: list | None = None) -> bool:
    """Renderiza `src` em OUT_DIR/name.mp4, grava capa e metadados e avisa o celular.

    Devolve False quando um clip automático é descartado por não mostrar o streamer.
    """
    def step(text: str) -> None:
        job["step"] = text
        activity.log(text, "info", job["label"])

    out = config.OUT_DIR / f"{name}.mp4"
    cam = Rect.parse(opts["cam"]) if opts.get("cam") else None
    try:
        path, title, detected = build_clip(str(src), out, title=opts.get("title"), cam=cam,
                                           credit=opts.get("credit"), log=step,
                                           known_cam=_known_cams.get(source),
                                           require_streamer=opts.get("kind") is not None,
                                           words=words)
    except NoStreamer:
        src.unlink(missing_ok=True)
        activity.log("Clip descartado: o streamer não aparece na câmera nesse trecho", "warn", job["label"])
        return False
    if detected:
        _known_cams[source] = detected     # mesma live, próxima vez: câmera no mesmo lugar
    duration = probe(path)["duration"]
    out.with_suffix(".jpg").write_bytes(frame_jpeg(path, duration / 2, width=360))
    out.with_suffix(".json").write_text(json.dumps({
        "name": name, "title": title, "credit": opts.get("credit"), "source": source,
        "kind": opts.get("kind"), "score": opts.get("score"), "rank": opts.get("rank"),
        "rating": None, "favorite": False,
        "duration": round(duration, 1), "created": time.time()}))
    job["clips"].append(name)
    src.unlink(missing_ok=True)   # o trecho bruto não é mais necessário
    activity.log(f"Clip pronto ({round(duration)} s)" + (f": {title}" if title else ""), "ok", job["label"])

    what = {"engraçado": "Momento engraçado", "impactante": "Momento impactante"}.get(opts.get("kind"), "Clip")
    keep = "1 hora" if CLIP_TTL == 3600 else f"{CLIP_TTL // 60} min"
    minutes = f" Fica disponível por {keep}." if CLIP_TTL else ""
    push.notify("Clip pronto", f"{title or what} · {job['label'][:40]}.{minutes}")
    return True


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

def _kept(clip: dict) -> bool:
    """Clip que não entra na contagem para ser apagado."""
    return bool(clip.get("favorite")) or clip.get("name") in publish.pending_clips()


def sweep(now: float | None = None) -> None:
    """Apaga clips vencidos e sobras antigas de processamento."""
    now = now or time.time()
    if CLIP_TTL:
        for meta in config.OUT_DIR.glob("clip_*.json"):
            try:
                data = json.loads(meta.read_text())
                expired = not _kept(data) and now - data["created"] > CLIP_TTL
            except (OSError, ValueError, KeyError):
                expired = now - meta.stat().st_mtime > CLIP_TTL
            if expired:
                for suffix in (".mp4", ".jpg", ".json"):
                    meta.with_suffix(suffix).unlink(missing_ok=True)
    for pattern in ("upload_*", "live_*.mp4", "vod_*", "zip_*"):
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
    unlimited: bool = False     # live gravada inteira: todos os bons momentos, sem teto
    title: str | None = None
    credit: str | None = None
    cam: str | None = None


@app.post("/api/clips/vod")
def create_vod(req: VodRequest) -> dict:
    url, opts = _check_url(req.url), _options(req.title, req.credit, req.cam)
    window = None
    if req.unlimited:
        req.auto = req.auto or 1
    elif req.auto:
        if not 1 <= req.auto <= 10:
            raise HTTPException(422, "Quantidade automática deve ficar entre 1 e 10.")
    else:
        try:
            window = (parse_time(req.start or ""), parse_time(req.end or ""))
        except ValueError:
            raise HTTPException(422, "Informe início e fim, ex.: 1:23:45.")
        if not 0 < window[1] - window[0] <= MAX_CLIP_SECONDS:
            raise HTTPException(422, f"O trecho deve ter entre 1 e {MAX_CLIP_SECONDS} segundos.")

    activity.log("Conferindo o link", "info", url)
    try:
        info = vod.probe(url)
    except RuntimeError as exc:
        activity.log(str(exc), "error", url)
        raise HTTPException(422, str(exc))
    activity.log("É uma live que está no ar: vou acompanhá-la ao vivo" if info["is_live"] else
                 "É uma live gravada" + (f" de {round(info['duration'] / 60)} min" if info["duration"] else ""),
                 "info", url)
    if info["is_live"]:
        # Link de live que está no ar: não há vídeo pronto para baixar; acompanha ao vivo.
        _start_live(url, auto=True, credit=opts["credit"])
        return {"live": True}
    if window and info["duration"] and window[0] >= info["duration"]:
        raise HTTPException(422, "O início informado passa do fim do vídeo.")

    return _submit("gravada", url, {"url": url, "window": window, "auto": req.auto, "opts": opts,
                                    "unlimited": req.unlimited, "duration": info["duration"]})


MAX_CANDIDATES = 12       # trechos baixados e avaliados, no máximo, por live gravada
MAX_UNLIMITED_CANDIDATES = 160    # no modo sem limite (uns 20 s de trabalho por candidato)


def _work_vod(job: dict) -> None:
    p, stamp = job["params"], job["id"]
    url = p["url"]
    if not p["auto"]:
        start, end = p["window"]
        job["step"] = "Baixando o trecho"
        raw = vod.download_section(url, start, end, config.WORK_DIR / f"vod_{stamp}_1.mp4")
        _render(job, raw, f"clip_{stamp}_1", p["opts"], url)
        return

    from ..detect.webcam import detect_webcam
    from ..highlights import rerank
    from ..highlights.audio import find_highlights
    from ..pipeline import _words

    def progress(done: int, total: int) -> None:
        job["step"] = f"Baixando áudio da live ({done * 100 // total}%)"

    job["step"] = "Baixando áudio da live"
    activity.log("Baixando o áudio da live inteira para achar os momentos", "info", job["label"])
    audio = vod.download_audio(url, config.WORK_DIR / f"vod_{stamp}.m4a", progress)
    job["step"] = "Analisando a live inteira"
    # 1ª etapa: onde o som se destacou. Pega candidatos de sobra, do mais forte ao mais fraco.
    wanted = p["auto"]
    limit = MAX_CANDIDATES
    if p.get("unlimited"):
        # Sem teto: a quantidade acompanha o tamanho da live, no mesmo ritmo por hora
        # escolhido para os clips automáticos ao vivo.
        hours = max((p.get("duration") or 3600) / 3600, 0.5)
        per_hour = SENSITIVITY[sensitivity()]
        wanted = max(3, round(hours * per_hour))
        limit = min(MAX_UNLIMITED_CANDIDATES, wanted * 2)
        activity.log(f"Sem limite: live de {hours:.1f} h, buscando cerca de {wanted} clips (~{per_hour} por hora)",
                     "info", job["label"])
    moments = sorted(find_highlights(str(audio), count=max(wanted * 4, limit)), key=lambda m: -m.score)[:limit]
    audio.unlink(missing_ok=True)
    if not moments:
        raise RuntimeError("Não achei nenhum momento marcante nessa live; tente o modo por intervalo.")
    activity.log(f"O áudio apontou {len(moments)} candidatos; vou avaliar cada um", "info", job["label"])

    # 2ª etapa: baixa cada candidato e olha o que ele mostra antes de escolher.
    candidates = []
    for i, moment in enumerate(moments, 1):
        job["step"] = f"Avaliando candidato {i} de {len(moments)}"
        raw = vod.download_section(url, moment.start, moment.end, config.WORK_DIR / f"vod_{stamp}_{i}.mp4")
        cam = detect_webcam(str(raw))
        at = f"{int(moment.peak // 3600)}:{int(moment.peak % 3600 // 60):02d}:{int(moment.peak % 60):02d}"
        if cam is None:           # abertura, intervalo, "já volto": sem streamer não há clip
            raw.unlink(missing_ok=True)
            activity.log(f"Candidato {i} ({at}) descartado: sem o streamer na câmera", "warn", job["label"])
            continue
        words = _words(str(raw))
        seen = rerank.visual(str(raw), cam)
        rank = rerank.final_score(moment.score, seen, rerank.speech(words), bool(words))
        candidates.append({"rank": rank, "raw": raw, "cam": cam, "words": words, "moment": moment, "index": i})
        activity.log(f"Candidato {i} ({at}): nota {rank}" + (" · tela parada" if seen["static"] else "")
                     + (" · streamer agitado" if seen["reaction"] > 0.4 else "")
                     + (" · fala de reação" if rerank.speech(words) > 0 else ""), "info", job["label"])
    candidates.sort(key=lambda c: -c["rank"])
    for loser in candidates[wanted:]:
        loser["raw"].unlink(missing_ok=True)
    if not candidates:
        raise RuntimeError("Os momentos mais barulhentos dessa live não mostram o streamer na câmera; "
                           "tente o modo por intervalo.")

    for n, c in enumerate(candidates[:wanted], 1):
        job["step"] = f"Montando clip {n} de {min(wanted, len(candidates))}"
        cam = c["cam"]
        opts = {**p["opts"], "kind": c["moment"].kind, "score": c["moment"].score, "rank": c["rank"],
                "cam": p["opts"].get("cam") or f"{cam.x},{cam.y},{cam.w},{cam.h}"}
        _render(job, c["raw"], f"clip_{stamp}_{c['index']}", opts, url, words=c["words"])


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
    ago: float = 0          # o trecho termina há quantos segundos (0 = agora)
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
    url, credit = _check_url(req.url), (req.credit or "").strip() or None
    try:
        manager.start(url, req.auto, credit)
    except LiveError as exc:
        if "offline" not in str(exc):
            raise HTTPException(409, str(exc))
        # Canal fora do ar não é erro: fica salvo e a gravação começa quando ele entrar.
        manager.remember(url, credit, enabled=True)
        return {**manager.status(), "notice": f"{display_name(url)} não está ao vivo agora. O canal ficou salvo: "
                                              "começo a gravar sozinho quando ele entrar."}
    return manager.status()


class Switch(BaseModel):
    on: bool


@app.post("/api/live/{session_id}/auto")
def live_auto(session_id: str, req: Switch) -> dict:
    """Liga ou desliga os clips automáticos de uma live, sem parar de acompanhá-la."""
    _live_session(session_id).set_auto(req.on)
    return manager.status()


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
    buffered = found.buffered()
    if buffered < 1:
        raise HTTPException(409, "O buffer ainda está vazio; aguarde alguns segundos.")
    if req.ago < 0 or req.ago + req.last > buffered + 6:
        raise HTTPException(422, f"Só há {round(buffered // 60)} min guardados dessa live; escolha um momento mais recente.")
    opts = _options(req.title, req.credit or found.credit, req.cam)
    # Corta já, na requisição, para pegar o momento exato do clique.
    return found.clip(req.last, opts, req.ago)


class Sensitivity(BaseModel):
    value: str


@app.post("/api/live/sensitivity")
def live_sensitivity(req: Sensitivity) -> dict:
    try:
        manager.set_sensitivity(req.value)
    except LiveError as exc:
        raise HTTPException(422, str(exc))
    return manager.status()


@app.post("/api/channels")
def channel_add(req: Channel) -> dict:
    try:
        manager.add_channel(_check_url(req.url), (req.credit or "").strip() or None)
    except LiveError as exc:
        raise HTTPException(409, str(exc))
    return manager.status()


class ChannelSwitch(BaseModel):
    url: str
    on: bool


@app.post("/api/channels/toggle")
def channel_toggle(req: ChannelSwitch) -> dict:
    manager.set_channel(req.url, req.on)
    return manager.status()


@app.post("/api/channels/start")
def channel_start(req: Channel) -> dict:
    """Tenta gravar o canal agora; se não houver live, só avisa."""
    notice = manager.start_channel(req.url)      # primeiro tenta; o estado devolvido já inclui a nova live
    return {**manager.status(), "notice": notice}


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
            clip["expires_in"] = (max(0, round(clip["created"] + CLIP_TTL - now))
                                  if CLIP_TTL and not _kept(clip) else None)
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


# ---------- favoritos e download em lote ----------

class Favorite(BaseModel):
    on: bool


@app.post("/api/clips/{name}/favorite")
def favorite_clip(name: str, req: Favorite) -> dict:
    """Favorito não é apagado pela faxina automática."""
    meta_path = _clip_file(name, ".json")
    meta = json.loads(meta_path.read_text())
    meta["favorite"] = req.on
    if not req.on:
        meta["created"] = time.time()      # ao desfavoritar, o prazo recomeça do zero
    meta_path.write_text(json.dumps(meta))
    return {"favorite": req.on}


@app.get("/api/clips/zip")
def clips_zip(names: str = "", favorites: bool = False) -> FileResponse:
    """Um .zip com os clips pedidos (todos, se nenhum for informado)."""
    wanted = [n for n in names.split(",") if n]
    clips = [c for c in list_clips()
             if (not wanted or c["name"] in wanted) and (not favorites or c.get("favorite"))]
    if not clips:
        raise HTTPException(404, "Nenhum clip para baixar.")
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = config.WORK_DIR / f"zip_{uuid.uuid4().hex[:10]}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:    # mp4 já é comprimido
        for i, clip in enumerate(clips, 1):
            label = re.sub(r"[^\w\- ]+", "", clip.get("title") or clip.get("kind") or "clip").strip()[:50]
            archive.write(_clip_file(clip["name"], ".mp4"), f"{i:02d} {label or 'clip'}.mp4")
    return FileResponse(path, media_type="application/zip", filename="viceclips.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


# ---------- saúde do servidor ----------

def _folder_bytes(folder: Path) -> int:
    """Tamanho da pasta. Arquivos temporários somem enquanto a gente conta; isso não é erro."""
    total = 0
    try:
        for item in folder.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _memory() -> dict:
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        kb = lambda key: int(info[key].split()[0]) * 1024
        return {"total": kb("MemTotal"), "available": kb("MemAvailable")}
    except (OSError, KeyError, ValueError):
        return {"total": 0, "available": 0}


@app.get("/api/health")
def health() -> dict:
    now = time.time()
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(config.WORK_DIR)
    cores = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    memory = _memory()
    with _lock:
        jobs = list(_jobs.values())
    waiting = [j for j in jobs if j["status"] == "fila"]
    running = next((j for j in jobs if j["status"] == "processando"), None)
    done = sorted((j for j in jobs if j.get("finished") and j.get("started") and j["status"] == "pronto"),
                  key=lambda j: j["finished"])[-20:]
    times = [j["finished"] - j["started"] for j in done]
    errors = sorted((j for j in jobs if j["status"] == "erro"), key=lambda j: j["created"], reverse=True)[:8]
    lives = manager.status()["sessions"]

    warnings = []
    if disk.free < max(2 * 1024 ** 3, disk.total * 0.1):
        warnings.append("Pouco espaço em disco: os próximos clips podem falhar.")
    if load > cores * 0.9:
        warnings.append("Processador no limite: os clips vão demorar mais para ficar prontos.")
    if memory["total"] and memory["available"] < memory["total"] * 0.1:
        warnings.append("Pouca memória livre.")
    if len(waiting) >= 5:
        warnings.append(f"{len(waiting)} clips esperando na fila: o servidor não está dando conta do ritmo.")
    if sum(now - j["created"] < 3600 for j in errors) >= 3:
        warnings.append("Vários clips falharam na última hora; veja os erros abaixo.")

    return {
        "warnings": warnings,
        "uptime": round(now - _started_at),
        "disk": {"total": disk.total, "free": disk.free, "clips": _folder_bytes(config.OUT_DIR),
                 "work": _folder_bytes(config.WORK_DIR)},
        "cpu": {"cores": cores, "load": round(load, 2), "percent": min(100, round(load / cores * 100))},
        "memory": memory,
        "queue": {"waiting": len(waiting), "running": running["step"] if running else None,
                  "running_label": running["label"] if running else None},
        "lives": {"active": sum(s["running"] for s in lives), "max": manager.status()["max"]},
        "renders": {"count": len(times), "average": round(sum(times) / len(times)) if times else None,
                    "slowest": round(max(times)) if times else None},
        "errors": [{"label": j["label"], "error": j["error"], "when": j["created"]} for j in errors],
    }


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


def _rating_records() -> list[dict]:
    try:
        return [json.loads(line) for line in _ratings_file().read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


@app.get("/api/ratings")
def ratings_summary() -> dict:
    records = _rating_records()
    latest = {record["name"]: record for record in records}   # vale a última de cada clip
    good = sum(r["good"] for r in latest.values())
    # Cada avaliação nova recalcula a exigência dos clips automáticos.
    return {"good": good, "bad": len(latest) - good, "calibration": calibration.apply(records)}


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


@app.post("/api/push/test")
def push_test() -> dict:
    devices = len(push.subscriptions())
    if not devices:
        raise HTTPException(409, "Nenhum aparelho com avisos ativados. Toque em \"Ativar avisos\" primeiro.")
    push.notify("Teste do ViceClips", "Se você está vendo isto, os avisos de clip pronto estão funcionando.")
    return {"devices": devices}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(req: PushSubscription) -> dict:
    push.unsubscribe(req.endpoint)
    return {"ok": True}


# ---------- publicação ----------

def _publisher() -> None:
    while not _halt.wait(30):
        try:
            publish.run_due()
        except Exception:
            pass


def _site(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _platform(name: str) -> str:
    if name not in publish.PLATFORMS:
        raise HTTPException(404, "Plataforma desconhecida.")
    return name


class NewPost(BaseModel):
    clip: str
    platforms: list[str]
    caption: str = ""
    when: float | None = None      # segundos desde 1970; vazio = agora


@app.get("/api/publish")
def publish_status(request: Request) -> dict:
    site = _site(request)
    return {"platforms": [{**p, "redirect": f"{site}/api/publish/{p['platform']}/callback"}
                          for p in publish.status()],
            "posts": sorted(publish.posts(), key=lambda p: p["when"], reverse=True)[:50]}


@app.get("/api/publish/{platform}/connect")
def publish_connect(platform: str, request: Request) -> dict:
    try:
        return {"url": publish.connect_url(_platform(platform), _site(request))}
    except publish.PublishError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/publish/{platform}/callback")
def publish_callback(platform: str, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    message = "conectado"
    try:
        if error or not code:
            raise publish.PublishError("A autorização foi cancelada ou recusada.")
        publish.finish_connect(_platform(platform), state, code)
    except publish.PublishError as exc:
        message = str(exc)
    return RedirectResponse("/?publicar=" + quote(message) + "#publicar")


@app.post("/api/publish/{platform}/disconnect")
def publish_disconnect(platform: str, request: Request) -> dict:
    publish.disconnect(_platform(platform))
    return publish_status(request)


@app.post("/api/posts")
def post_create(req: NewPost, request: Request) -> dict:
    meta = json.loads(_clip_file(req.clip, ".json").read_text())
    now = time.time()
    if req.when and not now - 300 <= req.when <= now + 30 * 86400:
        raise HTTPException(422, "Escolha uma data entre agora e os próximos 30 dias.")
    try:
        publish.schedule(req.clip, meta.get("title"), req.platforms, req.caption, req.when)
    except publish.PublishError as exc:
        raise HTTPException(409, str(exc))
    if not req.when or req.when <= now:
        threading.Thread(target=publish.run_due, daemon=True).start()   # publica já, sem esperar o ciclo
    return publish_status(request)


@app.delete("/api/posts/{post_id}")
def post_cancel(post_id: str, request: Request) -> dict:
    publish.cancel(post_id)
    return publish_status(request)


@app.get("/public/{expires}/{token}/{name}.mp4")
def public_video(expires: int, token: str, name: str) -> FileResponse:
    if not publish.valid_public(name, expires, token):
        raise HTTPException(404, "Link expirado.")
    return FileResponse(_clip_file(name, ".mp4"), media_type="video/mp4")


# ---------- páginas ----------

@app.get("/")
def index(request: Request) -> HTMLResponse:
    page = "index.html" if _authed(request) else "login.html"
    html = (STATIC / page).read_text(encoding="utf-8")
    # Cada arquivo leva a sua versão no endereço: depois de uma atualização o navegador é
    # obrigado a buscar o script e o estilo novos, nunca fica com a página nova e o script velho.
    for asset in ("app.js", "app.css"):
        version = int((STATIC / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/activity")
def activity_feed(after: int = 0) -> list[dict]:
    return activity.recent(after)


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Servido na raiz para o service worker valer para o site inteiro.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
