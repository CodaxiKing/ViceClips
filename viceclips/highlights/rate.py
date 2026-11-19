"""Exigência que se ajusta sozinha a cada live para entregar N clips por hora.

Um mínimo fixo em dB não serve: medido numa live real de 7 horas, "+10 dB acima do
normal" acontece 36 vezes por hora, e o que é alto num streamer é normal em outro.
Em vez disso, guardamos a força de todos os momentos da última hora e exigimos que
um novo momento esteja entre os N mais fortes dela.
"""
from collections import deque

WINDOW = 3600            # olha a última hora
LEARNING = 600           # nos primeiros 10 minutos ainda não há base: usa o valor inicial
INITIAL = 16.0           # dB; nos dados reais dá cerca de 10 por hora
FLOOR, CEILING = 8.0, 30.0
CHAT_BONUS = 3.0         # com o chat reagindo junto, exige 3 dB a menos
MERGE = 45               # picos a menos de 45 s um do outro são o mesmo momento
SENSITIVITY = {"poucos": 2, "equilibrado": 4, "muitos": 8}    # clips por hora, por live


class RateController:
    def __init__(self, per_hour: float = SENSITIVITY["equilibrado"]):
        self.per_hour = per_hour
        self.events: deque = deque()        # (segundo da live, força em dB) de todo momento visto
        self.start: float | None = None

    def observe(self, now: float, strength: float) -> None:
        """Registra um momento, tenha virado clip ou não."""
        if self.start is None:
            self.start = now
        if self.events and now - self.events[-1][0] < MERGE:
            # Mesmo trecho barulhento: conta como um momento só (o mais forte), senão uma
            # sequência de picos inflaria a régua e sairiam menos clips que o pedido.
            first, strongest = self.events[-1]
            self.events[-1] = (first, max(strongest, strength))
            return
        self.events.append((now, strength))
        while self.events and now - self.events[0][0] > WINDOW:
            self.events.popleft()

    def threshold(self, now: float) -> float:
        """Força mínima (dB acima do normal) para um momento virar clip agora."""
        if self.start is None or now - self.start < LEARNING:
            return INITIAL
        elapsed = min(now - self.start, WINDOW)
        recent = sorted((s for t, s in self.events if now - t <= WINDOW), reverse=True)
        keep = max(1, round(self.per_hour * elapsed / 3600))
        if len(recent) <= keep:
            return FLOOR                    # live calma: quase tudo que se destacar entra
        # Ponto entre o último que entraria e o primeiro que ficaria de fora.
        cut = (recent[keep - 1] + recent[keep]) / 2
        return min(max(cut, FLOOR), CEILING)

    def accepts(self, now: float, strength: float, chat_reacted: bool = False) -> bool:
        return strength >= self.threshold(now) - (CHAT_BONUS if chat_reacted else 0.0)
