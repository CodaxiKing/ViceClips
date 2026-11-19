"""Live ao vivo: buffer circular em disco + corte dos últimos N segundos.

`record` fica rodando e mantém os últimos ~BUFFER_SEGMENTS * SEGMENT_SECONDS
segundos da live em segmentos .ts. `cut_last` monta um mp4 com o final do buffer
sem interromper a gravação.
"""
import subprocess
import sys
from pathlib import Path

from ..media import run

SEGMENT_SECONDS = 6
BUFFER_SEGMENTS = 100   # ~10 minutos


def resolve_stream_url(url: str) -> str:
    """URL HLS direta da live (ou devolve a entrada se já for arquivo/stream)."""
    if Path(url).exists() or url.endswith(".m3u8"):
        return url
    out = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-g", "-f", "b[height<=1080]/b", url],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"Não consegui abrir a live: {out.stderr.strip()[-500:]}")
    return out.stdout.strip().splitlines()[0]


def record(url: str, buffer_dir: Path, realtime: bool = False) -> subprocess.Popen:
    """Inicia a gravação do buffer. `realtime` simula live a partir de um arquivo."""
    buffer_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if realtime:
        cmd += ["-re"]
    cmd += ["-i", resolve_stream_url(url), "-c", "copy", "-f", "segment",
            "-segment_time", str(SEGMENT_SECONDS), "-segment_wrap", str(BUFFER_SEGMENTS),
            "-segment_list", str(buffer_dir / "buffer.m3u8"),
            "-segment_list_size", str(BUFFER_SEGMENTS - 2), "-segment_list_flags", "live",
            str(buffer_dir / "seg_%03d.ts")]
    return subprocess.Popen(cmd)


def _buffer_segments(playlist: Path) -> list[tuple[Path, float]]:
    """(arquivo, duração) dos segmentos já fechados, do mais antigo ao mais novo."""
    segments, duration = [], 0.0
    for line in playlist.read_text().splitlines():
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":")[1].rstrip(","))
        elif line and not line.startswith("#"):
            segments.append((playlist.parent / line, duration))
    return segments


def cut_last(buffer_dir: Path, seconds: float, out: Path) -> Path:
    """Corta os últimos `seconds` já gravados no buffer para um mp4."""
    playlist = buffer_dir / "buffer.m3u8"
    segments = _buffer_segments(playlist) if playlist.exists() else []
    if not segments:
        raise FileNotFoundError(f"Buffer vazio em {buffer_dir}; rode `viceclips live` antes.")
    tail, total = [], 0.0
    for seg, dur in reversed(segments):
        tail.insert(0, seg)
        total += dur
        if total >= seconds:
            break
    out.parent.mkdir(parents=True, exist_ok=True)
    concat = out.with_suffix(".txt")
    concat.write_text("".join(f"file '{seg.resolve()}'\n" for seg in tail))
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-ss", f"{max(0.0, total - seconds):.2f}", "-c", "copy",
             "-bsf:a", "aac_adtstoasc", str(out)])
    finally:
        concat.unlink()
    return out
