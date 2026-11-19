"""Download direto de lives gravadas em HLS (Twitch e semelhantes).

Em vez de deixar o ffmpeg ler a rede, baixamos a playlist e só os segmentos que
interessam por HTTP comum e montamos o arquivo localmente. É mais rápido para
pegar um trecho no meio de uma live de horas e funciona atrás de proxy.
"""
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from ..media import run

TIMEOUT = 60
PARALLEL = 8
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except OSError as exc:
            last = exc
    raise RuntimeError(f"Não consegui baixar um pedaço do vídeo ({last}).")


def stream_url(page_url: str, formats: str) -> str | None:
    """URL da playlist HLS de um vídeo, ou None se o site não entrega HLS."""
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "-g", "-f", formats, page_url],
                         capture_output=True, text=True, timeout=120)
    url = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    return url if ".m3u8" in url else None


def parse_playlist(text: str, base: str) -> tuple[str | None, list[tuple[str, float, float]]]:
    """Devolve (URL do cabeçalho fMP4 ou None, [(URL do segmento, início, duração)])."""
    init, segments, clock, duration = None, [], 0.0, 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
            init = urljoin(base, line.split('URI="', 1)[1].split('"', 1)[0])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-STREAM-INF"):
            raise ValueError("playlist mestre: escolha uma qualidade antes")
        elif line and not line.startswith("#"):
            segments.append((urljoin(base, line), clock, duration))
            clock += duration
    return init, segments


def _fetch_segments(init: str | None, segments: list, out: Path, progress=None) -> None:
    """Baixa em paralelo e grava em ordem, sem guardar o vídeo inteiro na memória."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh, ThreadPoolExecutor(PARALLEL) as pool:
        if init:
            fh.write(_get(init))
        for i in range(0, len(segments), PARALLEL * 2):
            batch = segments[i: i + PARALLEL * 2]
            for data in pool.map(lambda seg: _get(seg[0]), batch):
                fh.write(data)
            if progress:
                progress(min(i + len(batch), len(segments)), len(segments))


def download_section(playlist_url: str, start: float, end: float, out: Path) -> Path:
    """Baixa só os segmentos que cobrem [start, end] e corta o trecho exato."""
    init, segments = parse_playlist(_get(playlist_url).decode("utf-8", "replace"), playlist_url)
    wanted = [s for s in segments if s[1] + s[2] > start and s[1] < end]
    if not wanted:
        raise RuntimeError("O trecho pedido está fora da duração do vídeo.")
    raw = out.with_suffix(".part.mp4" if init else ".part.ts")
    try:
        _fetch_segments(init, wanted, raw)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start - wanted[0][1]):.2f}", "-i", str(raw),
             "-t", f"{end - start:.2f}", "-c", "copy", "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart", str(out)])
    finally:
        raw.unlink(missing_ok=True)
    return out


def download_all(playlist_url: str, out: Path, progress=None) -> Path:
    """Baixa a playlist inteira (usado para o áudio da live completa)."""
    init, segments = parse_playlist(_get(playlist_url).decode("utf-8", "replace"), playlist_url)
    if not segments:
        raise RuntimeError("A playlist do vídeo veio vazia.")
    _fetch_segments(init, segments, out, progress)
    return out
