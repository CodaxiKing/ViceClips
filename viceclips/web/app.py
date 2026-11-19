"""Interface web do ViceClips: API + página única servida em /.

Um worker processa os jobs em fila (um render por vez). O estado dos jobs vive em
memória; os clips prontos ficam em OUT_DIR com um .json (metadados) e um .jpg (capa).
"""
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config
from ..config import Rect
from ..ingest import live, vod
from ..media import frame_jpeg, parse_time, probe
from ..pipeline import build_clip

STATIC = Path(__file__).parent / "static"
MAX_CLIP_SECONDS = 180
CLIP_NAME = re.compile(r"^clip_[0-9a-z_]+$")

app = FastAPI(title="ViceClips")
_executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_live: dict = {"proc": None, "url": None, "started": None}


# ---------- jobs ----------

def _submit(kind: str, label: str, work) -> dict:
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "label": label, "status": "fila",
           "step": "Na fila", "error": None, "clips": [], "created": time.time()}
    with _lock:
        _jobs[job["id"]] = job
    _executor.submit(_run, job, work)
    return job


def _run(job: dict, work) -> None:
    job["status"] = "processando"
    try:
        work(job)
        job["status"], job["step"] = "pronto", "Pronto"
    except Exception as exc:
        job["status"], job["step"] = "erro", "Erro"
        job["error"] = str(exc)[-600:]


def _render(job: dict, src: Path, name: str, opts: dict, source: str) -> None:
    """Renderiza `src` em OUT_DIR/name.mp4 e grava capa e metadados."""
    def step(text: str) -> None:
        job["step"] = text

    out = config.OUT_DIR / f"{name}.mp4"
    path, title, _ = build_clip(str(src), out, title=opts.get("title"), cam=opts.get("cam"),
                                credit=opts.get("credit"), log=step)
    duration = probe(path)["duration"]
    out.with_suffix(".jpg").write_bytes(frame_jpeg(path, duration / 2, width=360))
    out.with_suffix(".json").write_text(json.dumps({
        "name": name, "title": title, "credit": opts.get("credit"), "source": source,
        "duration": round(duration, 1), "created": time.time()}))
    job["clips"].append(name)


def _options(title: str | None, credit: str | None, cam: str | None) -> dict:
    try:
        rect = Rect.parse(cam) if cam and cam.strip() else None
    except ValueError:
        raise HTTPException(422, "Câmera deve ser X,Y,L,A (quatro números).")
    return {"title": (title or "").strip() or None, "credit": (credit or "").strip() or None,
            "cam": rect}


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

    def work(job: dict) -> None:
        stamp = job["id"]
        windows = [window]
        if req.auto:
            from ..highlights.audio import find_highlights
            job["step"] = "Baixando áudio da live"
            audio = vod.download_audio(url, config.WORK_DIR / f"vod_{stamp}.m4a")
            job["step"] = "Procurando melhores momentos"
            windows = [(s, e) for s, e, _ in find_highlights(str(audio), count=req.auto)]
            if not windows:
                raise RuntimeError("Nenhum pico de áudio encontrado; use o modo por intervalo.")
        for i, (start, end) in enumerate(windows, 1):
            job["step"] = f"Baixando trecho {i}/{len(windows)}"
            raw = vod.download_section(url, start, end, config.WORK_DIR / f"vod_{stamp}_{i}.mp4")
            _render(job, raw, f"clip_{stamp}_{i}", opts, url)

    return _submit("gravada", url, work)


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
    return _submit("arquivo", label,
                   lambda job: _render(job, raw, f"clip_{job['id']}", opts, label))


class LiveStart(BaseModel):
    url: str


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
    return {"running": running, "failed": failed, "url": _live["url"],
            "started": _live["started"], "buffered": round(buffered),
            "capacity": live.SEGMENT_SECONDS * live.BUFFER_SEGMENTS}


@app.get("/api/live")
def live_status() -> dict:
    return _live_status()


@app.post("/api/live/start")
def live_start(req: LiveStart) -> dict:
    url = _check_url(req.url)
    if _live_status()["running"]:
        raise HTTPException(409, "Já existe uma live sendo gravada; pare-a antes.")
    for old in _live_dir().glob("*"):
        old.unlink()
    try:
        _live["proc"] = live.record(url, _live_dir())
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    _live.update(url=url, started=time.time())
    return _live_status()


@app.post("/api/live/stop")
def live_stop() -> dict:
    proc = _live["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)
    return _live_status()


@app.post("/api/live/clip")
def live_clip(req: LiveClip) -> dict:
    status = _live_status()
    if not 1 <= req.last <= MAX_CLIP_SECONDS:
        raise HTTPException(422, f"Escolha entre 1 e {MAX_CLIP_SECONDS} segundos.")
    if status["buffered"] < 1:
        raise HTTPException(409, "O buffer ainda está vazio; aguarde alguns segundos.")
    opts = _options(req.title, req.credit, req.cam)
    job_id = uuid.uuid4().hex[:10]
    # Corta já, na requisição, para pegar o momento exato do clique.
    raw = live.cut_last(_live_dir(), req.last, config.WORK_DIR / f"live_{job_id}.mp4")
    return _submit("ao vivo", status["url"] or "live",
                   lambda job: _render(job, raw, f"clip_{job['id']}", opts, status["url"] or "live"))


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)


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
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
