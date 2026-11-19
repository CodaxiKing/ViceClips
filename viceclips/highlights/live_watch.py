"""Clip automático na live ao vivo: vigia o buffer e avisa quando o áudio dispara.

O pico (grito, risada, susto) é comparado com o volume normal da própria live.
Depois de um pico, espera alguns segundos de "depois" e pede o corte que cobre
do "antes" até ali.
"""
import subprocess
import threading
from pathlib import Path

import numpy as np

from ..ingest.live import _buffer_segments
from .audio import loudness_per_second

PRE_SECONDS = 20     # quanto do "antes" do pico entra no clip
POST_SECONDS = 10    # quanto do "depois"


class PeakDetector:
    """Recebe o volume segundo a segundo e devolve os instantes que são picos."""

    def __init__(self, warmup: int = 45, cooldown: int = 60, z: float = 2.5,
                 ratio: float = 1.8, history: int = 600):
        self.warmup, self.cooldown, self.z, self.ratio, self.history = warmup, cooldown, z, ratio, history
        self.rms: list[float] = []
        self.offset = 0          # segundos já descartados do início
        self.checked = 0         # até onde (tempo absoluto) já foi avaliado
        self.last_peak = -10**9

    @property
    def total(self) -> int:
        return self.offset + len(self.rms)

    def feed(self, values) -> list[int]:
        self.rms.extend(float(v) for v in values)
        if len(self.rms) > self.history:
            drop = len(self.rms) - self.history
            self.rms, self.offset = self.rms[drop:], self.offset + drop
        arr = np.array(self.rms)
        peaks = []
        if len(arr):
            median, std = float(np.median(arr)), float(arr.std()) + 1e-6
            for i in range(max(self.checked - self.offset, 0), len(arr)):
                t = i + self.offset
                if t < self.warmup or t - self.last_peak < self.cooldown:
                    continue
                if arr[i] > median * self.ratio and (arr[i] - median) / std > self.z:
                    peaks.append(t)
                    self.last_peak = t
        self.checked = self.total
        return peaks


class LiveWatcher(threading.Thread):
    """Lê os segmentos novos do buffer e chama on_highlight(segundos_a_cortar)."""

    def __init__(self, buffer_dir: Path, on_highlight, poll: float = 3.0):
        super().__init__(daemon=True)
        self.buffer_dir, self.on_highlight, self.poll = buffer_dir, on_highlight, poll
        self.detector = PeakDetector()
        self.pending: list[int] = []
        self.clips = 0
        self._seen: set[tuple[str, int]] = set()
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

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
                self.pending += self.detector.feed(loudness_per_second(str(seg)))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue  # segmento sumiu ou veio corrompido: segue para o próximo
        now = self.detector.total
        for peak in [p for p in self.pending if now >= p + POST_SECONDS]:
            self.pending.remove(peak)
            self.on_highlight(float(now - (peak - PRE_SECONDS)))
            self.clips += 1

    def run(self) -> None:
        while not self._halt.wait(self.poll):
            try:
                self.step()
            except Exception:
                pass  # o vigia nunca deve derrubar a gravação
