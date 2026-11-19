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
from ..highlights.rate import SENSITIVITY
from ..ingest import live, vod
from . import activity

MAX_LIVES = int(os.environ.get("VICECLIPS_MAX_LIVES", "4"))
POLL_SECONDS = 90          # intervalo para conferir se os canais entraram ao vivo
KEEP_ENDED_SECONDS = 600   # live manual encerrada some da tela depois disso
MAX_CLIP_SECONDS = 180
MAX_RECONNECTS = 30        # quedas do gravador toleradas numa mesma live
SHORT_RUN = 20             # gravador que morre antes disso, 3 vezes seguidas, não tem conserto


def _settings_file() -> Path:
    return config.WORK_DIR / "settings.json"


def sensitivity() -> str:
    """Quantos clips automáticos por hora: "poucos", "equilibrado" ou "muitos"."""
    try:
        value = json.loads(_settings_file().read_text()).get("sensitivity")
    except (OSError, ValueError):
        value = None
    return value if value in SENSITIVITY else "equilibrado"


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
        self._stopped = self._ended = self.failed = False
        self.reconnects = self._short_runs = 0
        self.proc = live.record(url, self.dir)     # levanta RuntimeError se o link não abrir
        self._proc_started = time.time()
        threading.Thread(target=self._keep_alive, daemon=True).start()
        if auto:
            meter, channel = None, twitch_channel(url)
            if channel:   # o chat da Twitch reforça a análise; sem ele segue só o áudio
                meter = ChatMeter()
                self.chat = TwitchChat(channel, meter)
                self.chat.start()
            self.watcher = LiveWatcher(self.dir, self._highlight, chat=meter,
                                       on_event=lambda text, level="info": activity.log(text, level, self.name))
            self.watcher.rate.per_hour = SENSITIVITY[sensitivity()]
            self.watcher.start()

    def _highlight(self, seconds: float, kind: str, score: float) -> None:
        self.clip(min(seconds, MAX_CLIP_SECONDS),
                  {"title": None, "credit": self.credit, "cam": None, "kind": kind, "score": score})

    @property
    def running(self) -> bool:
        return not self._ended

    def _check(self) -> None:
        """Se o gravador caiu e a live continua no ar, reconecta.

        O gravador às vezes encerra sozinho, sem erro, na troca do anúncio inicial da
        Twitch para a transmissão. Sem isto a live sumia e nada mais era clipado.
        """
        if self._ended or self.proc.poll() is None:
            return
        if self._stopped:
            self._ended = True
            return
        self._short_runs = self._short_runs + 1 if time.time() - self._proc_started < SHORT_RUN else 0
        if self.reconnects >= MAX_RECONNECTS or self._short_runs >= 3:
            self.failed = self.buffered() == 0
            self._ended = True
            return
        try:
            self.proc = live.record(self.url, self.dir)    # falha se o canal saiu do ar
        except RuntimeError:
            self._ended = True                              # a live acabou de verdade
            return
        self.reconnects += 1
        self._proc_started = time.time()
        activity.log(f"A gravação caiu e foi retomada ({self.reconnects}ª vez)", "warn", self.name)

    def _keep_alive(self) -> None:
        while not self._ended:
            time.sleep(3)
            try:
                self._check()
            except Exception:
                pass

    def buffered(self) -> float:
        playlist = self.dir / "buffer.m3u8"
        return sum(d for _, d in live._buffer_segments(playlist)) if playlist.exists() else 0.0

    def clip(self, seconds: float, opts: dict, ago: float = 0.0) -> dict:
        """Corta `seconds` do buffer (terminando `ago` segundos atrás) e manda para a fila."""
        job_id = uuid.uuid4().hex[:10]
        raw = live.cut_last(self.dir, seconds, config.WORK_DIR / f"live_{job_id}.mp4", ago)
        return self._on_clip(self, raw, opts, job_id)

    def stop(self) -> None:
        self._stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        for worker in (self.watcher, self.chat):
            if worker:
                worker.stop()
        self._ended = True
        self.ended_at = self.ended_at or time.time()

    def status(self) -> dict:
        buffered = self.buffered()
        running = self.running
        return {
            "id": self.id, "url": self.url, "name": self.name, "origin": self.origin,
            "running": running,
            # O gravador não conseguiu gravar nada: o link não é uma live que dê para ler.
            "failed": self.failed, "reconnects": self.reconnects,
            "auto": bool(self.watcher), "auto_clips": self.watcher.clips if self.watcher else 0,
            "chat": bool(self.chat and self.chat.connected),
            "analysed": self.watcher.total if self.watcher else 0,
            "lift": round(self.watcher.lift, 1) if self.watcher else None,
            "peak_lift": round(self.watcher.peak_lift, 1) if self.watcher else None,
            "needed": round(self.watcher.rate.threshold(self.watcher.total), 1) if self.watcher else None,
            "per_hour": self.watcher.rate.per_hour if self.watcher else None,
            "waiting": bool(self.watcher and self.watcher.pending),
            "started": self.started, "uptime": round((self.ended_at or time.time()) - self.started),
            "buffered": round(buffered),
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
                activity.log(f"Não consegui abrir a live: {exc}", "error", display_name(url))
                raise LiveError(str(exc))
            self.sessions[session.id] = session
            activity.log("Começou a gravar a live" + (" (canal acompanhado entrou ao vivo)" if origin == "canal" else ""),
                         "ok", session.name)
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

    def set_sensitivity(self, value: str) -> None:
        if value not in SENSITIVITY:
            raise LiveError("Sensibilidade desconhecida.")
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        _settings_file().write_text(json.dumps({"sensitivity": value}))
        for session in list(self.sessions.values()):       # vale já para as lives em andamento
            if session.watcher:
                session.watcher.rate.per_hour = SENSITIVITY[value]
        activity.log(f"Sensibilidade dos clips automáticos: {value} (cerca de {SENSITIVITY[value]} por hora, por live)", "ok")

    def status(self) -> dict:
        with self._lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.started)
        return {"sessions": [s.status() for s in sessions], "max": MAX_LIVES,
                "sensitivity": sensitivity(), "sensitivities": SENSITIVITY,
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
        activity.log("Canal cadastrado; vou conferir a cada 90 s se entrou ao vivo", "ok", display_name(url))
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
            if session.ended_at is None:
                failed = session.failed
                activity.log("A gravação falhou: o link não entregou vídeo" if failed else "A live terminou",
                             "error" if failed else "info", session.name)
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
