"""Clip automático na live ao vivo.

Vigia o buffer, mede o áudio de cada segmento novo (ver analysis.py) e, quando
aparece um momento, espera a reação esfriar e pede o corte. Se houver chat da
Twitch, ele reforça a nota, ajuda a classificar e também dispara clips sozinho
quando explode sem o áudio ter subido (o streamer rindo baixo, por exemplo).
"""
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..ingest.live import _buffer_segments
from . import analysis
from .analysis import FUNNY, IMPACT
from .chat import ChatMeter
from .rate import SENSITIVITY, RateController

WARMUP = analysis.WARMUP
COOLDOWN = 45        # intervalo mínimo entre dois clips automáticos
NEAR_MISS = 3.0       # dB: descarte que chegou a isso da régua aparece na Atividade
BUDGET_WINDOW = 1800  # trava de segurança: em meia hora...
BUDGET_MAX = 10       # ...nunca mais que isto, aconteça o que acontecer
HISTORY = 600        # segundos de medidas mantidos em memória
CHAT_RATIO = 3.0     # chat N vezes mais rápido que o normal = explosão
CHAT_MIN_MESSAGES = 8
CHAT_LAG = 6         # o chat reage alguns segundos depois do que aconteceu


class LiveWatcher(threading.Thread):
    """Chama on_highlight(segundos_a_cortar, tipo, nota) a cada momento encontrado."""

    def __init__(self, buffer_dir: Path, on_highlight, chat: ChatMeter | None = None,
                 poll: float = 3.0, on_event=None):
        super().__init__(daemon=True)
        self.buffer_dir, self.on_highlight, self.chat, self.poll = buffer_dir, on_highlight, chat, poll
        self.feats = np.zeros((0, 3), np.float32)
        self.offset = 0                       # segundos já descartados do início
        self.checked = 0                      # até onde (tempo da live) já procurou momentos
        self.last_trigger = -10**9
        self.pending: dict | None = None      # momento esperando a reação esfriar
        self.clips = 0
        self.on_event = on_event or (lambda text, level="info": None)
        self.lift = 0.0                       # dB acima do normal no último segundo analisado
        self.peak_lift = 0.0                  # o maior desde o último clip
        self.recent: list[tuple[int, float]] = []  # (segundo, nota) dos clips recentes
        self.rate = RateController(SENSITIVITY["equilibrado"])    # quantos clips por hora queremos
        self.clock: list[tuple[int, float]] = []   # (segundo da live, relógio) para alinhar o chat
        self._seen: set[tuple[str, int]] = set()
        self._halt = threading.Event()

    @property
    def total(self) -> int:
        return self.offset + len(self.feats)

    def stop(self) -> None:
        self._halt.set()

    def feed(self, feats: np.ndarray) -> None:
        self.feats = np.concatenate([self.feats, feats])
        if len(self.feats) > HISTORY:
            drop = len(self.feats) - HISTORY
            self.feats, self.offset = self.feats[drop:], self.offset + drop
        self.clock = (self.clock + [(self.total, time.time())])[-200:]

    def _wall_time(self, second: int) -> float:
        """Relógio aproximado em que o segundo `second` da live foi gravado."""
        mark, wall = min(self.clock, key=lambda item: abs(item[0] - second))
        return wall + (second - mark)

    def _chat_burst(self, second: int) -> dict | None:
        if not self.chat or not self.clock:
            return None
        return self.chat.burst(self._wall_time(second))

    def evaluate(self) -> None:
        if len(self.feats) < 12:
            return
        s = analysis.score(self.feats)
        self.lift = float(s["lift"][-1])
        self.peak_lift = max(self.peak_lift, float(s["lift"][-6:].max()))

        if self.pending is None:
            first = max(self.checked, WARMUP, self.last_trigger + COOLDOWN) - self.offset
            for i in range(max(first, 0), len(self.feats)):
                if analysis.is_moment(s, i, analysis.MILD_LIFT):
                    self.pending = {"peak": i + self.offset, "score": float(s["score"][i]), "audio": True}
                    break
            if self.pending is None:
                self._chat_only_trigger()
            self.checked = self.total

        if self.pending is None:
            return
        peak = self.pending["peak"] - self.offset
        if peak < 0:                                   # saiu do histórico: desiste
            self.pending = None
            return
        # Se a reação ainda está crescendo, acompanha o pico mais alto.
        upto = min(len(self.feats), peak + 6)
        best = peak + int(np.argmax(s["score"][peak:upto]))
        if s["score"][best] > self.pending["score"]:
            self.pending.update(peak=best + self.offset, score=float(s["score"][best]))
            peak = best
        if not analysis.reaction_over(s, peak):
            return

        start, _ = analysis.window(s, peak)
        kind = analysis.classify(s, peak) if self.pending["audio"] else self.pending["kind"]
        score = self.pending["score"]
        burst = self._chat_burst(self.pending["peak"])
        chat_reacted = bool(burst and burst["ratio"] >= 2 and burst["messages"] >= CHAT_MIN_MESSAGES)
        if chat_reacted:
            score += min(burst["ratio"], 6)
            if burst["laughs"] >= 0.3:
                kind = FUNNY
            elif burst["hype"] >= 0.3:
                kind = IMPACT
        moment, audio_only = self.pending["peak"], self.pending["audio"]
        self.pending = None
        # A régua se ajusta a esta live: o momento precisa estar entre os mais fortes da
        # última hora para caber na quantidade de clips por hora pedida (ver rate.py).
        needed = self.rate.threshold(moment)
        strength = float(s["lift"][peak: peak + 6].max()) if audio_only else needed
        accepted = self.rate.accepts(moment, strength, chat_reacted)
        self.rate.observe(moment, strength)
        if not accepted:
            # Numa live real são dezenas de descartes por hora; só os que chegaram perto vão
            # para a tela de Atividade, senão ela vira ruído.
            if strength >= needed - NEAR_MISS:
                self.on_event(f"Quase: momento de +{strength:.0f} dB ficou de fora (nesta live só entram os de "
                              f"+{needed:.0f} dB para cima, os {self.rate.per_hour:g} mais fortes por hora)", "warn")
            return
        if not self._within_budget(moment, score):
            self.on_event("Momento descartado: limite de segurança de clips por meia hora atingido", "warn")
            return
        self.last_trigger = moment
        self.recent.append((moment, score))
        self.peak_lift = 0.0
        self.on_event(f"Momento {kind} encontrado (nota {score:.1f}): cortando o clip", "ok")
        self.on_highlight(float(len(self.feats) - start), kind, round(score, 1))
        self.clips += 1

    def _within_budget(self, moment: int, score: float) -> bool:
        """Numa live agitada, fica só com os melhores em vez de clipar tudo."""
        self.recent = [(t, sc) for t, sc in self.recent if moment - t < BUDGET_WINDOW]
        return len(self.recent) < BUDGET_MAX

    def _chat_only_trigger(self) -> None:
        """Chat explodiu sem pico de áudio: clipa o que veio logo antes."""
        second = self.total - WINDOW_BEHIND
        if second < max(WARMUP, self.last_trigger + COOLDOWN):
            return
        burst = self._chat_burst(second)
        if not burst or burst["ratio"] < CHAT_RATIO or burst["messages"] < CHAT_MIN_MESSAGES:
            return
        if max(burst["laughs"], burst["hype"]) < 0.3:
            return   # chat rápido, mas não é reação (comando, spam, enquete)
        kind = FUNNY if burst["laughs"] >= burst["hype"] else IMPACT
        self.pending = {"peak": max(second - CHAT_LAG, self.offset), "score": min(burst["ratio"], 6) + 3,
                        "audio": False, "kind": kind}

    def step(self) -> None:
        playlist = self.buffer_dir / "buffer.m3u8"
        if not playlist.exists():
            return
        for seg, _ in _buffer_segments(playlist):
            try:
                key = (seg.name, seg.stat().st_mtime_ns)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.feed(analysis.audio_features(str(seg)))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue  # segmento sumiu ou veio corrompido: segue para o próximo
        self.evaluate()

    def run(self) -> None:
        while not self._halt.wait(self.poll):
            try:
                self.step()
            except Exception:
                pass  # o vigia nunca deve derrubar a gravação


WINDOW_BEHIND = 12   # o chat-only olha a janela que terminou há pouco
