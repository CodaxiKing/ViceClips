"""Localiza a facecam do streamer dentro do frame da live.

Estratégia:
1. Amostra N frames espalhados pelo clip e roda detecção de rosto em cada um.
2. O rosto do streamer fica parado no overlay; rostos do jogo se movem. Fica o
   grupo de detecções que aparece no mesmo lugar na maioria dos frames.
3. Procura as quatro bordas do overlay em volta do rosto: linhas retas que ficam
   no mesmo lugar em todos os frames, enquanto o jogo muda por trás.
4. Lado cuja borda não foi confirmada é cortado rente ao rosto. O recorte nunca
   pode passar da câmera: é melhor um close do streamer do que uma faixa do jogo
   aparecendo no painel da câmera.
"""
from pathlib import Path

import cv2
import numpy as np

from ..config import Rect

MIN_PRESENCE = 0.4      # fração mínima de frames em que o rosto precisa aparecer
FACE_MODEL = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")   # OpenCV Zoo, licença MIT
FACE_SCORE = 0.7        # confiança mínima de cada detecção
LINE_CONTRAST = 3.0     # borda = gradiente N vezes acima do típico da região
LINE_MIN = 12.0         # e acima deste mínimo absoluto
INSET = 3               # pixels para dentro da borda, para não pegar a moldura


def _sample_frames(path: str, samples: int) -> list[np.ndarray]:
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    for pos in np.linspace(total * 0.05, total * 0.95, samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos))
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()
    return frames


def _face_boxes(frames: list[np.ndarray]) -> list[tuple[int, int, int, int, int]]:
    """Rostos em cada frame, como (índice do frame, x, y, lado, lado)."""
    h, w = frames[0].shape[:2]
    min_side = max(24, h // 20)
    boxes = []
    try:
        # YuNet: bem mais preciso que o Haar, que "via" rosto em cadeira vazia e cenário.
        detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), FACE_SCORE)
        for i, frame in enumerate(frames):
            _, found = detector.detect(frame)
            for x, y, bw, bh, *_ in (found if found is not None else []):
                side = int(max(bw, bh) * 1.1)      # caixa quadrada, um pouco folgada
                if side >= min_side:
                    boxes.append((i, int(x + bw / 2 - side / 2), int(y + bh / 2 - side / 2), side, side))
    except cv2.error:
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        for i, frame in enumerate(frames):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for box in cascade.detectMultiScale(gray, 1.1, 5, minSize=(min_side, min_side)):
                boxes.append((i, *map(int, box)))
    return boxes


def _stable_face(frames: list[np.ndarray]) -> tuple[int, int, int, int] | None:
    boxes = _face_boxes(frames)
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


def _static_gradients(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Gradiente médio entre os frames: o que é fixo (bordas do overlay) se destaca."""
    gx = sum(np.abs(cv2.Sobel(f, cv2.CV_32F, 1, 0, ksize=3)) for f in frames) / len(frames)
    gy = sum(np.abs(cv2.Sobel(f, cv2.CV_32F, 0, 1, ksize=3)) for f in frames) / len(frames)
    return gx, gy


def _find_line(profile: np.ndarray, lo: int, hi: int, nearest_end: str) -> int | None:
    """Borda clara em profile[lo:hi] mais próxima do rosto, ou None se não houver.

    `nearest_end` diz de que lado da faixa o rosto está ("hi" ou "lo"). Ficar com a
    borda mais interna garante que o recorte não passa da câmera.
    """
    lo, hi = max(lo, 0), min(hi, len(profile))
    if hi - lo < 3:
        return None
    seg = profile[lo:hi]
    peak, typical = float(seg.max()), float(np.median(seg))
    if peak < max(LINE_CONTRAST * typical, LINE_MIN):
        return None
    hits = np.flatnonzero(seg >= max(LINE_CONTRAST * typical, 0.6 * peak)) + lo
    return int(hits.max() if nearest_end == "hi" else hits.min())


def _borders(gx, gy, face, rows: slice, cols: slice) -> dict:
    """Procura as quatro bordas; `rows`/`cols` dizem em que faixa uma borda deve aparecer."""
    fx, fy, fw, fh = face
    col_profile = gx[rows, :].mean(axis=0)   # bordas verticais (esquerda/direita)
    row_profile = gy[:, cols].mean(axis=1)   # bordas horizontais (topo/base)
    # Faixas de busca: a câmera nunca fica muito maior que o rosto, e o queixo pode
    # estar quase encostado na base (streamer bem enquadrado de perto).
    reach_x, reach_y = int(2.2 * fw), int(2.2 * fh)
    return {
        "left": _find_line(col_profile, fx - reach_x, fx - fw // 12, "hi"),
        "right": _find_line(col_profile, fx + fw + fw // 12, fx + fw + reach_x, "lo"),
        "top": _find_line(row_profile, fy - int(1.5 * fh), fy - fh // 12, "hi"),
        "bottom": _find_line(row_profile, fy + fh + fh // 12, fy + fh + reach_y, "lo"),
    }


def detect_webcam(path: str, samples: int = 16) -> Rect | None:
    """Retângulo da facecam em pixels do vídeo de origem, ou None se não achar."""
    frames = _sample_frames(path, samples)
    if not frames:
        return None
    face = _stable_face(frames)
    if face is None:
        return None
    fx, fy, fw, fh = face
    H, W = frames[0].shape[:2]
    gx, gy = _static_gradients([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames])

    # Corte rente ao rosto: com certeza está dentro da câmera.
    tight = {"left": fx - int(0.2 * fw), "right": fx + fw + int(0.2 * fw),
             "top": fy - int(0.1 * fh), "bottom": fy + fh + int(0.3 * fh)}

    # 1ª passada: bordas visíveis na altura/largura do rosto.
    rows = slice(max(fy - fh // 4, 0), min(fy + fh + fh // 4, H))
    cols = slice(max(fx - fw // 4, 0), min(fx + fw + fw // 4, W))
    found = _borders(gx, gy, face, rows, cols)

    # 2ª passada: uma borda de verdade atravessa a câmera inteira. Medir na extensão
    # toda (já estimada) destaca a moldura e apaga detalhes do cenário e do jogo.
    box = {side: found[side] if found[side] is not None else tight[side] for side in tight}
    pad_y, pad_x = (box["bottom"] - box["top"]) // 10, (box["right"] - box["left"]) // 10
    rows = slice(max(box["top"] + pad_y, 0), min(box["bottom"] - pad_y, H))
    cols = slice(max(box["left"] + pad_x, 0), min(box["right"] - pad_x, W))
    refined = _borders(gx, gy, face, rows, cols)

    inward = {"left": INSET, "top": INSET, "right": -INSET, "bottom": -INSET}
    edges = {}
    for side in tight:
        line = refined[side] if refined[side] is not None else found[side]
        edges[side] = line + inward[side] if line is not None else tight[side]

    left, top = max(edges["left"], 0), max(edges["top"], 0)
    right, bottom = min(edges["right"], W), min(edges["bottom"], H)
    return Rect(left, top, right - left, bottom - top)
