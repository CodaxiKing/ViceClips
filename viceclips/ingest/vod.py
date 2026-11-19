"""Download de trechos de lives gravadas (Twitch VOD, YouTube, Kick...) via yt-dlp."""
import subprocess
import sys
from pathlib import Path

YTDLP = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings"]
VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"


def friendly_error(stderr: str) -> str:
    """Troca a saída do yt-dlp por uma frase que diga o que fazer."""
    lines = [l for l in stderr.strip().splitlines() if l.startswith("ERROR")]
    raw = (lines[-1] if lines else stderr.strip()[-300:]).removeprefix("ERROR: ")
    low = raw.lower()
    if "offline" in low or "not currently live" in low:
        return ("O canal está offline. Para clipar uma live que já acabou, use o link do "
                "vídeo gravado (ex.: twitch.tv/videos/...).")
    if "unsupported url" in low:
        return "Não reconheci esse link. Use o link da live ou do vídeo na Twitch, YouTube ou Kick."
    if "private" in low or "subscriber" in low or "sign in" in low or "login" in low:
        return "Esse vídeo é privado ou exige login/assinatura, então não consigo baixar."
    if "does not exist" in low or "not found" in low or "unavailable" in low:
        return "Não encontrei esse vídeo. Confira se o link está certo e se ele ainda existe."
    return f"Não consegui baixar: {raw}"


def _ytdlp(args: list[str], timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(YTDLP + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("O site do vídeo demorou demais para responder. Tente de novo.")
    if proc.returncode != 0:
        raise RuntimeError(friendly_error(proc.stderr))
    return proc.stdout


def probe(url: str) -> dict:
    """Olha o link sem baixar: é live ao vivo? quanto dura?"""
    out = _ytdlp(["--skip-download", "--print", "%(is_live)s\t%(duration)s", url], timeout=60)
    is_live, duration = (out.strip().splitlines() or ["NA\tNA"])[0].split("\t")[:2]
    return {"is_live": is_live == "True",
            "duration": float(duration) if duration.replace(".", "", 1).isdigit() else None}


def download_section(url: str, start: float, end: float, out: Path) -> Path:
    """Baixa só o intervalo [start, end] (segundos) em 1080p ou menos."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _ytdlp(["-f", VIDEO_FORMAT, "--download-sections", f"*{start:.1f}-{end:.1f}",
            "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "-o", str(out), url])
    return out


def download_audio(url: str, out: Path) -> Path:
    """Baixa só o áudio da live inteira (leve) para a busca de highlights."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _ytdlp(["-f", "ba/b", "-x", "--audio-format", "m4a", "-o", str(out), url])
    return out
