"""Ajusta a exigência dos clips automáticos a partir das avaliações 👍/👎.

Cada clip automático tem uma nota (o quanto o momento se destacou). Com avaliações
suficientes, procuramos a nota mínima que mantém os clips que você aprovou e corta
os que reprovou.
"""
from . import analysis

MIN_RATINGS = 15        # avaliações de clips automáticos antes de mexer em qualquer coisa
MIN_BAD = 3             # reprovações mínimas para subir a exigência
KEEP_GOOD = 0.7         # nunca cortar mais de 30% dos clips aprovados
LOWEST, HIGHEST = 0.8, 3.0   # limites em relação ao padrão


def usable(records: list[dict]) -> list[dict]:
    """Só clips automáticos (com nota); vale a última avaliação de cada um."""
    latest = {}
    for record in records:
        if record.get("auto") and record.get("score") is not None:
            latest[record["name"]] = record
    return list(latest.values())


def suggest(records: list[dict], default: float = analysis.DEFAULT_MIN_SCORE) -> dict:
    rated = usable(records)
    good = sorted(r["score"] for r in rated if r["good"])
    bad = sorted(r["score"] for r in rated if not r["good"])
    result = {"min_score": default, "default": default, "calibrated": False,
              "rated": len(rated), "needed": max(0, MIN_RATINGS - len(rated))}
    if len(rated) < MIN_RATINGS:
        return result

    if len(bad) < MIN_BAD:
        # Quase tudo aprovado: dá para ser um pouco menos exigente e gerar mais clips.
        if len(good) >= MIN_RATINGS and not bad:
            result.update(min_score=round(default * LOWEST, 1), calibrated=True)
        return result

    # Nota de corte que maximiza (aprovados mantidos - reprovados mantidos).
    best, best_gain = default, None
    for cut in sorted({default, *good, *bad}):
        kept_good = sum(s >= cut for s in good)
        if good and kept_good / len(good) < KEEP_GOOD:
            break
        gain = kept_good - sum(s >= cut for s in bad)
        if best_gain is None or gain > best_gain:
            best, best_gain = cut, gain
    best = min(max(best, default * LOWEST), default * HIGHEST)
    result.update(min_score=round(float(best), 1), calibrated=round(float(best), 1) != default)
    return result


def apply(records: list[dict]) -> dict:
    """Recalcula e passa a usar a nova nota mínima na análise."""
    result = suggest(records)
    analysis.MIN_SCORE = result["min_score"]
    return result
