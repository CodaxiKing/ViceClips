"""Helpers finos em volta de ffmpeg/ffprobe."""
import json
import subprocess


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} falhou:\n{proc.stderr[-2000:]}")


def probe(path: str) -> dict:
    """Retorna width, height e duration (s) do primeiro stream de vídeo."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    s = data["streams"][0]
    return {"width": s["width"], "height": s["height"],
            "duration": float(data["format"].get("duration", 0) or 0)}


def parse_time(text: str) -> float:
    """'90', '1:30' ou '1:02:03' -> segundos."""
    secs = 0.0
    for part in str(text).split(":"):
        secs = secs * 60 + float(part)
    return secs


def frame_jpeg(path: str, at: float, width: int = 768) -> bytes:
    """Um frame JPEG (bytes) no instante `at`."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", path, "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
        capture_output=True, check=True).stdout
