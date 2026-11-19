"""Análise de áudio para achar momentos impactantes e engraçados.

Para cada segundo do áudio são medidas três coisas:

- nível:   volume em dB;
- agudos:  fração da energia acima de 1,5 kHz (grito e risada sobem aqui; fala normal não);
- rajada:  o quanto o volume oscila dentro do segundo (risada e gritaria vêm em rajadas).

Cada medida é comparada com o "normal" dos minutos anteriores da própria live, então um
streamer que fala alto o tempo todo não dispara clip, e um susto num trecho calmo dispara.
O mesmo código serve à live gravada (arquivo inteiro) e à live ao vivo (segmento a segmento).
"""
import subprocess
from dataclasses import dataclass

import numpy as np

RATE = 8000
FRAMES_PER_SECOND = 20
BASELINE_SECONDS = 180      # janela do "normal"
DEFAULT_MIN_SCORE = 5.0
MIN_SCORE = DEFAULT_MIN_SCORE   # abaixo disso não é momento; as avaliações do usuário ajustam
MIN_LIFT = 10.0             # dB acima do normal exigidos no pico (~3x mais alto)
MILD_LIFT = 6.0             # basta isso quando o chat também reagiu
MIN_GAP = 45                # segundos mínimos entre dois momentos
WARMUP = 60                 # segundos de live antes de confiar no "normal"

FUNNY, IMPACT = "engraçado", "impactante"


@dataclass
class Moment:
    start: float
    end: float
    peak: float
    score: float
    kind: str


def features_from_pcm(pcm: np.ndarray) -> np.ndarray:
    """PCM mono em RATE Hz -> matriz (segundos, 3) com [nível dB, agudos, rajada]."""
    secs = len(pcm) // RATE
    if secs == 0:
        return np.zeros((0, 3), np.float32)
    x = pcm[: secs * RATE].astype(np.float32).reshape(secs, RATE)
    level = 10 * np.log10((x ** 2).mean(axis=1) + 1.0)
    frames = x.reshape(secs, FRAMES_PER_SECOND, -1)
    burst = (10 * np.log10((frames ** 2).mean(axis=2) + 1.0)).std(axis=1)
    spectrum = np.abs(np.fft.rfft(x, axis=1)) ** 2          # 1 bin = 1 Hz
    bright = spectrum[:, 1500:].sum(axis=1) / (spectrum[:, 80:].sum(axis=1) + 1.0)
    return np.stack([level, bright, burst], axis=1).astype(np.float32)


def audio_features(path: str, chunk_seconds: int = 120) -> np.ndarray:
    """Lê o áudio em blocos (não carrega horas de live na memória de uma vez)."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    parts, want = [], chunk_seconds * RATE * 2
    while True:
        raw = proc.stdout.read(want)
        if not raw:
            break
        parts.append(features_from_pcm(np.frombuffer(raw[: len(raw) // 2 * 2], np.int16)))
    proc.wait()
    return np.concatenate(parts) if parts else np.zeros((0, 3), np.float32)


def _trailing_z(x: np.ndarray, floor: float) -> tuple[np.ndarray, np.ndarray]:
    """Desvio de cada segundo em relação à mediana dos BASELINE_SECONDS anteriores."""
    n = len(x)
    base, z = np.zeros(n, np.float32), np.zeros(n, np.float32)
    for t in range(n):
        past = x[max(0, t - BASELINE_SECONDS): t]
        if len(past) < 10:
            base[t] = x[t]
            continue
        med = np.median(past)
        mad = np.median(np.abs(past - med)) * 1.4826
        base[t] = med
        z[t] = (x[t] - med) / max(mad, floor)
    return z, base


def score(feats: np.ndarray) -> dict:
    """Nota de "momento" por segundo, mais os componentes usados para classificar."""
    level, bright, burst = feats[:, 0], feats[:, 1], feats[:, 2]
    z_level, baseline = _trailing_z(level, floor=1.5)
    z_bright, _ = _trailing_z(bright, floor=0.03)
    z_burst, _ = _trailing_z(burst, floor=0.8)

    loud = np.clip(z_level, 0, 6)
    recent = np.array([level[max(0, t - 5): t].mean() if t else level[0] for t in range(len(level))])
    jump = np.clip((level - recent) / 6.0, 0, 2)             # subida brusca (susto, explosão)
    colour = 1 + 0.25 * np.clip(z_bright, 0, 4) + 0.25 * np.clip(z_burst, 0, 4)
    instant = loud * colour + jump * (loud > 0.5)
    # Reação de verdade dura alguns segundos: soma o segundo com os dois seguintes.
    sustained = instant + np.roll(instant, -1) * 0.6 + np.roll(instant, -2) * 0.4
    sustained[-2:] = instant[-2:]
    # Medido em lives reais: o volume oscila ±9 dB o tempo todo sem nada acontecer.
    # Por isso, além da nota, o pico precisa ficar MIN_LIFT dB acima do normal.
    return {"score": sustained, "level": level, "baseline": baseline, "lift": level - baseline,
            "loud": loud, "jump": jump, "burst": np.clip(z_burst, 0, 4),
            "bright": np.clip(z_bright, 0, 4)}


def is_moment(s: dict, t: int, lift: float = MIN_LIFT) -> bool:
    return bool(s["score"][t] >= MIN_SCORE and s["lift"][t] >= lift)


def classify(s: dict, peak: int) -> str:
    """Palpite pelo áudio: risada se prolonga em rajadas; impacto é uma pancada curta.

    É só um palpite (fala animada também vem em rajadas); quando há chat, ele decide.
    """
    after = slice(peak, min(peak + 6, len(s["loud"])))
    laughing = int(((s["lift"][after] > 5.0) & (s["burst"][after] > 0.8)).sum())
    return FUNNY if laughing >= 3 else IMPACT


def window(s: dict, peak: int, max_after: int = 15) -> tuple[int, int]:
    """Início numa pausa da fala antes do momento; fim quando a reação esfria.

    `max_after` é até onde o fim pode ir depois do pico, se a reação não esfriar antes.
    """
    level, n = s["level"], len(s["level"])
    lo, hi = max(0, peak - 25), max(0, peak - 12)
    start = lo + int(np.argmin(level[lo:hi])) if hi > lo else lo
    end = min(n, peak + max_after)
    calm = level < s["baseline"] + 3.0
    for t in range(peak + 5, min(n - 1, peak + max_after)):
        if calm[t] and calm[t + 1]:
            end = t + 1
            break
    return start, end


def reaction_over(s: dict, peak: int) -> bool:
    """Ao vivo: já dá para cortar? (a reação esfriou, ou passou o limite de espera)"""
    n = len(s["level"])
    if n >= peak + 16:
        return True
    calm = s["level"] < s["baseline"] + 3.0
    return any(calm[t] and calm[t + 1] for t in range(peak + 5, n - 1))


def pick_moments(feats: np.ndarray, count: int, warmup: int = WARMUP) -> list[Moment]:
    """Os `count` melhores momentos do áudio inteiro, em ordem cronológica."""
    if len(feats) < warmup + 5:
        return []
    s = score(feats)
    ranked, chosen = np.argsort(s["score"])[::-1], []
    for t in ranked:
        if len(chosen) == count or s["score"][t] < MIN_SCORE:
            break
        if not is_moment(s, int(t)):
            continue
        if t < warmup or any(abs(int(t) - c) < MIN_GAP for c in chosen):
            continue
        chosen.append(int(t))
    moments = []
    for peak in sorted(chosen):
        start, end = window(s, peak)
        moments.append(Moment(float(start), float(end), float(peak),
                              round(float(s["score"][peak]), 1), classify(s, peak)))
    return moments
