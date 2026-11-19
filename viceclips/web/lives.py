"""Lives acompanhadas ao mesmo tempo e canais que começam a gravar sozinhos.

Cada live é uma LiveSession com buffer, vigia de momentos e (na Twitch) chat
próprios. O LiveManager guarda as sessões, a lista de canais cadastrados e um
laço que confere periodicamente quais canais entraram ao vivo.
"""
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .. import config
from ..highlights.chat import ChatMeter, TwitchChat, twitch_channel
from ..highlights.live_watch import LiveWatcher
from ..ingest import live, vod

MAX_LIVES = int(os.environ.get("VICECLIPS_MAX_LIVES", "4"))
POLL_SECONDS = 90          # intervalo para conferir se os canais entraram ao vivo
KEEP_ENDED_SECONDS = 600   # live manual encerrada some da tela depois disso
MAX_CLIP_SECONDS = 180


class LiveError(Exception):
    """Erro com mensagem pronta para mostrar ao usuário."""


def display_name(url: str) -> str:
    return twitch_channel(url) or url.split("://", 1)[-1].split("?")[0].rstrip("/")


class LiveSession:
    def __init__(self, url: str, auto: bool, credit: str | None, origin: str, on_clip):
        self.id = uuid.uuid4().hex[:8]
        self.url, self.auto, self.credit, self.origin = url, auto, credit, origin
        self.name = display_name(url)
        self.dir = config.WORK_DIR / "live" / self.id
        self.started = time.time()
        self.ended_at: float | None = None
        self.watcher: LiveWatcher | None = None
        self.chat: TwitchChat | None = None
        self._on_clip = on_clip
        self.proc = live.record(url, self.dir)     # levanta RuntimeError se o link não abrir
        if auto:
            meter, channel = None, twitch_channel(url)
            if channel:   # o chat da Twitch reforça a análise; sem ele segue só o áudio
                meter = ChatMeter()
                self.chat = TwitchChat(channel, meter)
                self.chat.start()
            self.watcher = LiveWatcher(self.dir, self._highlight, chat=meter)
            self.watcher.start()

    def _highlight(self, seconds: float, kind: str, score: float) -> None:
        self.clip(min(seconds, MAX_CLIP_SECONDS),
                  {"title": None, "credit": self.credit, "cam": None, "kind": kind, "score": score})

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def buffered(self) -> float:
        playlist = self.dir / "buffer.m3u8"
        return sum(d for _, d in live._buffer_segments(playlist)) if playlist.exists() else 0.0

    def clip(self, seconds: float, opts: dict) -> dict:
        """Corta os últimos `seconds` do buffer e manda para a fila de render."""
        job_id = uuid.uuid4().hex[:10]
        raw = live.cut_last(self.dir, seconds, config.WORK_DIR / f"live_{job_id}.mp4")
        return self._on_clip(self, raw, opts, job_id)

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        for worker in (self.watcher, self.chat):
            if worker:
                worker.stop()
        self.ended_at = self.ended_at or time.time()

    def status(self) -> dict:
        buffered = self.buffered()
        running = self.running
        return {
            "id": self.id, "url": self.url, "name": self.name, "origin": self.origin,
            "running": running,
            # ffmpeg saiu com erro sem gravar nada: o link não é uma live que dê para ler.
            "failed": bool(not running and self.proc.returncode and buffered == 0),
            "auto": bool(self.watcher), "auto_clips": self.watcher.clips if self.watcher else 0,
            "chat": bool(self.chat and self.chat.connected),
            "started": self.started, "buffered": round(buffered),
            "capacity": live.SEGMENT_SECONDS * live.BUFFER_SEGMENTS,
        }


class LiveManager:
    def __init__(self, on_clip):
        self.sessions: dict[str, LiveSession] = {}
        self._on_clip = on_clip
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._channel_live: dict[str, bool] = {}

    # ----- sessões -----

    def start(self, url: str, auto: bool = True, credit: str | None = None,
              origin: str = "manual") -> LiveSession:
        with self._lock:
            active = [s for s in self.sessions.values() if s.running]
            if any(display_name(s.url) == display_name(url) for s in active):
                raise LiveError("Essa live já está sendo acompanhada.")
            if len(active) >= MAX_LIVES:
                raise LiveError(f"Limite de {MAX_LIVES} lives ao mesmo tempo. Pare uma para acompanhar outra.")
            # A mesma live parada/falha na lista dá lugar à nova tentativa.
            for old in [s for s in self.sessions.values() if display_name(s.url) == display_name(url)]:
                self.remove(old.id)
            try:
                session = LiveSession(url, auto, credit, origin, self._on_clip)
            except RuntimeError as exc:
                raise LiveError(str(exc))
            self.sessions[session.id] = session
            return session

    def get(self, session_id: str) -> LiveSession | None:
        return self.sessions.get(session_id)

    def stop(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session:
            session.stop()

    def remove(self, session_id: str) -> None:
        with self._lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.stop()
            shutil.rmtree(session.dir, ignore_errors=True)

    def status(self) -> dict:
        with self._lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.started)
        return {"sessions": [s.status() for s in sessions], "max": MAX_LIVES,
                "channels": [{**c, "name": display_name(c["url"]),
                              "live": self._channel_live.get(c["url"], False)}
                             for c in self.channels()]}

    # ----- canais cadastrados -----

    def _channels_file(self) -> Path:
        return config.WORK_DIR / "channels.json"

    def channels(self) -> list[dict]:
        try:
            return json.loads(self._channels_file().read_text())
        except (OSError, ValueError):
            return []

    def _save_channels(self, channels: list[dict]) -> None:
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._channels_file().write_text(json.dumps(channels))

    def add_channel(self, url: str, credit: str | None) -> None:
        with self._lock:
            channels = self.channels()
            if any(display_name(c["url"]) == display_name(url) for c in channels):
                raise LiveError("Esse canal já está cadastrado.")
            if len(channels) >= 20:
                raise LiveError("Limite de 20 canais cadastrados.")
            self._save_channels(channels + [{"url": url, "credit": credit}])
        threading.Thread(target=self.check_channels, daemon=True).start()   # confere já

    def remove_channel(self, url: str) -> None:
        with self._lock:
            self._save_channels([c for c in self.channels() if c["url"] != url])
            self._channel_live.pop(url, None)

    def check_channels(self) -> None:
        """Começa a acompanhar os canais cadastrados que entraram ao vivo."""
        for channel in self.channels():
            url = channel["url"]
            with self._lock:
                watching = any(s.running and display_name(s.url) == display_name(url)
                               for s in self.sessions.values())
            if watching:
                self._channel_live[url] = True
                continue
            try:
                is_live = vod.probe(url)["is_live"]
            except RuntimeError:
                is_live = False        # offline, link inválido ou site fora do ar: tenta de novo depois
            self._channel_live[url] = is_live
            if is_live:
                try:
                    self.start(url, auto=True, credit=channel.get("credit"), origin="canal")
                except LiveError:
                    pass               # limite de lives atingido: fica para a próxima rodada

    def reap(self) -> None:
        """Tira da lista as lives que acabaram."""
        now = time.time()
        for session in list(self.sessions.values()):
            if session.running:
                continue
            session.ended_at = session.ended_at or now
            for worker in (session.watcher, session.chat):
                if worker:
                    worker.stop()
            # Live de canal cadastrado some logo, para poder recomeçar na próxima transmissão.
            keep = 120 if session.origin == "canal" else KEEP_ENDED_SECONDS
            if now - session.ended_at > keep:
                self.remove(session.id)

    # ----- laço de fundo -----

    def _loop(self) -> None:
        while not self._halt.wait(POLL_SECONDS):
            try:
                self.reap()
                self.check_channels()
            except Exception:
                pass

    def start_background(self) -> None:
        shutil.rmtree(config.WORK_DIR / "live", ignore_errors=True)   # buffers de execuções antigas
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self.check_channels, daemon=True).start()

    def shutdown(self) -> None:
        self._halt.set()
        for session in list(self.sessions.values()):
            session.stop()
