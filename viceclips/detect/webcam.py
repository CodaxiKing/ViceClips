"""Localiza a facecam do streamer dentro do frame da live.

Estratégia:
1. Amostra N frames espalhados pelo clip e roda detecção de rosto em cada um.
2. O rosto do streamer fica parado no overlay; rostos do jogo se movem. Fica o
   grupo de detecções que aparece no mesmo lugar na maioria dos frames.
3. A partir do rosto, chuta um retângulo 16:9 por proporção e encaixa cada lado na
   borda fixa mais próxima (linha reta que persiste em quase todos os frames).
"""
import cv2
import numpy as np

from ..config import Rect

MIN_PRESENCE = 0.4      # fração mínima de frames em que o rosto precisa aparecer
EDGE_PERSISTENCE = 0.45  # quão "fixa" uma linha precisa ser para contar como borda


def _sample_frames(path: str, samples: int) -> list[np.ndarray]:
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    for pos in np.linspace(total * 0.05, total * 0.95, samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos))
        ok, frame = cap.read()
        if ok:
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    return frames


def _stable_face(frames: list[np.ndarray]) -> tuple[int, int, int, int] | None:
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = frames[0].shape
    min_side = max(24, h // 20)
    boxes = []  # (índice do frame, x, y, w, h)
    for i, gray in enumerate(frames):
        for box in cascade.detectMultiScale(gray, 1.1, 5, minSize=(min_side, min_side)):
            boxes.append((i, *map(int, box)))
    if not boxes:
        return None

    # Para cada detecção, conta em quantos frames distintos há outra detecção próxima.
    best, best_frames = None, 0
    for _, x, y, bw, bh in boxes:
        cx, cy = x + bw / 2, y + bh / 2
        near = [b for b in boxes
                if abs(b[1] + b[3] / 2 - cx) < bw * 0.6 and abs(b[2] + b[4] / 2 - cy) < bh * 0.6]
        n = len({b[0] for b in near})
        if n > best_frames:
            best, best_frames = near, n
    if best_frames < MIN_PRESENCE * len(frames):
        return None
    x, y, bw, bh = (int(np.median([b[k] for b in best])) for k in (1, 2, 3, 4))
    return x, y, bw, bh


def _edge_persistence(frames: list[np.ndarray]) -> np.ndarray:
    acc = np.zeros(frames[0].shape, np.float32)
    for gray in frames:
        edges = cv2.Canny(gray, 60, 160)
        acc += cv2.dilate(edges, np.ones((3, 3), np.uint8)) > 0
    return acc / len(frames)


def _find_line(profile: np.ndarray, lo: int, hi: int, guess: int) -> int:
    """Linha persistente em profile[lo:hi] mais próxima do chute; sem linha, o chute."""
    lo, hi = max(lo, 0), min(hi, len(profile))
    hits = np.flatnonzero(profile[lo:hi] >= EDGE_PERSISTENCE) + lo
    if len(hits) == 0:
        return guess
    return int(hits[np.argmin(np.abs(hits - guess))])


def detect_webcam(path: str, samples: int = 16) -> Rect | None:
    """Retângulo da facecam em pixels do vídeo de origem, ou None se não achar."""
    frames = _sample_frames(path, samples)
    if not frames:
        return None
    face = _stable_face(frames)
    if face is None:
        return None
    fx, fy, fw, fh = face
    H, W = frames[0].shape

    # Chute por proporção: overlay 16:9 com o rosto no terço superior central.
    gw = int(fw * 3.2)
    gh = int(gw * 9 / 16)
    left, top = fx + fw // 2 - gw // 2, fy - int(fh * 0.6)
    right, bottom = left + gw, top + gh

    # Refina cada lado com bordas persistentes perto do rosto.
    persist = _edge_persistence(frames)
    rows = slice(max(fy - fh // 4, 0), min(fy + fh + fh // 4, H))
    cols = slice(max(fx - fw // 4, 0), min(fx + fw + fw // 4, W))
    col_profile = persist[rows, :].mean(axis=0)
    row_profile = persist[:, cols].mean(axis=1)

    left = _find_line(col_profile, fx - 3 * fw, fx - fw // 3, left)
    right = _find_line(col_profile, fx + fw + fw // 3, fx + 4 * fw, right)
    top = _find_line(row_profile, fy - 2 * fh, fy - fh // 5, top)
    bottom = _find_line(row_profile, fy + fh + fh // 3, fy + 5 * fh, bottom)

    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, W), min(bottom, H)
    return Rect(left, top, right - left, bottom - top)
