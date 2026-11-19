"""Download de trechos de lives gravadas (Twitch VOD, YouTube, Kick...) via yt-dlp."""
import sys
from pathlib import Path

from ..media import run

YTDLP = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings"]
VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"


def download_section(url: str, start: float, end: float, out: Path) -> Path:
    """Baixa só o intervalo [start, end] (segundos) em 1080p ou menos."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(YTDLP + ["-f", VIDEO_FORMAT, "--download-sections", f"*{start:.1f}-{end:.1f}",
                 "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
                 "-o", str(out), url])
    return out


def download_audio(url: str, out: Path) -> Path:
    """Baixa só o áudio da live inteira (leve) para a busca de highlights."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(YTDLP + ["-f", "ba/b", "-x", "--audio-format", "m4a", "-o", str(out), url])
    return out
