"""Segunda etapa da escolha em live gravada: olhar cada candidato antes de decidir.

A análise de áudio (analysis.py) só diz onde o som subiu, e som alto também é
música de abertura, alerta de inscrito e tela de "já volto". Aqui cada candidato
já baixado ganha uma nota pelo que mostra:

- jogo:    a tela do jogo está se mexendo? (menu, pausa e tela de espera são paradas)
- reação:  o streamer se mexeu de repente na câmera? (susto, risada, comemoração)
- fala:    ele disse algo de reação? ("meu deus", "que isso", palavrão, risada)

A nota final combina isso com a nota do áudio; tela parada derruba o candidato.
"""
import re

import cv2
import numpy as np

from ..config import Rect

SAMPLE_FPS = 4
STATIC_GAME = 1.2        # diferença média entre frames abaixo disso = tela parada
CALM_MOTION, LIVELY_MOTION = 3.5, 11.0
REACTION = re.compile(
    r"meu deus|ai meu|caralho|porra|puta|merda|cacete|que isso|qu[eé] que [eé] isso|n[aã]o acredito"
    r"|olha isso|olha s[oó]|socorro|que susto|nossa|eita|vixe|mentira|imposs[ií]vel|morri|\bmano\b"
    r"|k{3,}|(ha){2,}|n[aã]o n[aã]o|vai vai|\bgol\b|consegui|bora|absurdo|maluco|doido|insano", re.I)


def visual(path: str, cam: Rect | None, width: int = 240) -> dict:
    """Movimento do jogo e agitação do streamer na câmera ao longo do clip."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, round(fps / SAMPLE_FPS))
    game_diffs, cam_diffs, prev_game, prev_cam, index = [], [], None, None, 0
    while cap.grab():
        if index % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if cam:
                face = cv2.resize(gray[cam.y: cam.y + cam.h, cam.x: cam.x + cam.w], (96, 72)).astype(np.float32)
                if prev_cam is not None:
                    cam_diffs.append(float(np.abs(face - prev_cam).mean()))
                prev_cam = face
                gray = gray.copy()
                gray[cam.y: cam.y + cam.h, cam.x: cam.x + cam.w] = 0      # o jogo é tudo menos a câmera
            small = cv2.resize(gray, (width, int(gray.shape[0] * width / gray.shape[1]))).astype(np.float32)
            if prev_game is not None:
                game_diffs.append(float(np.abs(small - prev_game).mean()))
            prev_game = small
        index += 1
    cap.release()
    if not game_diffs:
        return {"game_motion": 0.0, "static": True, "reaction": 0.0}
    game = float(np.median(game_diffs))
    # Agitação: quanto ele se mexe nos momentos mais movimentados do trecho. Medido em lives
    # reais: ~3,5 assistindo parado, ~9 falando e gesticulando.
    lively = float(np.percentile(cam_diffs, 90)) if len(cam_diffs) >= 8 else 0.0
    reaction = float(np.clip((lively - CALM_MOTION) / (LIVELY_MOTION - CALM_MOTION), 0, 1))
    return {"game_motion": round(game, 2), "static": game < STATIC_GAME, "reaction": round(reaction, 2)}


def speech(words: list) -> float:
    """0 a 1: quanto da fala do streamer é reação."""
    text = " ".join(w.text for w in words)
    hits = len(REACTION.findall(text))
    return float(min(1.0, hits / 3))


def final_score(audio_score: float, seen: dict, said: float, has_speech: bool) -> float:
    """Nota de 0 a ~10 para ordenar os candidatos."""
    score = min(audio_score, 30) / 30 * 4            # o áudio continua valendo, mas não manda sozinho
    score += seen["reaction"] * 3 + said * 2
    score += 1.0 if has_speech else 0.0              # streamer falando > só barulho do jogo/alerta
    if seen["static"]:
        score *= 0.25                                # menu, pausa, "já volto": quase nunca é clip
    return round(score, 2)
