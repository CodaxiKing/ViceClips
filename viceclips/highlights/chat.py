"""Chat da Twitch como sinal de momento: quando o chat explode, algo aconteceu.

Leitura anônima (sem login) pelo IRC público da Twitch. Se a conexão falhar, a
análise segue só com o áudio; o chat nunca derruba a gravação.
"""
import re
import socket
import ssl
import threading
import time

LAUGH = re.compile(r"k{3,}|(ha){2,}|(he){2,}|(hu){2,}|rs{2,}|\blol\b|lmao|kekw|\blul\b|omegalul"
                   r"|icant|[😂🤣💀😹]", re.I)
HYPE = re.compile(r"pog|\bomg\b|\bwtf\b|caraca|caralho|\bmds\b|meu deus|\bnossa\b|clipa|clipe"
                  r"|[!?]{3,}|\bw{3,}\b|[😱🔥😳]", re.I)
CHANNEL = re.compile(r"twitch\.tv/(?!videos/)([A-Za-z0-9_]{3,25})(?:[/?#]|$)")
HOST, PORT = "irc.chat.twitch.tv", 6697
WINDOW = 10        # segundos somados para medir uma "explosão" do chat
BASELINE = 300     # segundos de histórico para o ritmo normal


def twitch_channel(url: str) -> str | None:
    match = CHANNEL.search(url)
    return match.group(1).lower() if match else None


class ChatMeter:
    """Conta mensagens por segundo e diz quando o ritmo foge do normal."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[int, list[int]] = {}   # segundo -> [total, risada, hype]

    def add(self, when: float, text: str) -> None:
        with self._lock:
            bucket = self._buckets.setdefault(int(when), [0, 0, 0])
            bucket[0] += 1
            bucket[1] += bool(LAUGH.search(text))
            bucket[2] += bool(HYPE.search(text))
            for old in [s for s in self._buckets if s < when - BASELINE - 60]:
                del self._buckets[old]

    def burst(self, when: float) -> dict:
        """Compara os WINDOW segundos a partir de `when` com o ritmo anterior."""
        with self._lock:
            def total(start: int, column: int) -> int:
                return sum(self._buckets.get(s, [0, 0, 0])[column] for s in range(start, start + WINDOW))

            now = int(when)
            messages, laughs, hype = total(now, 0), total(now, 1), total(now, 2)
            history = [total(start, 0) for start in range(now - BASELINE, now - WINDOW, WINDOW)
                       if any(s in self._buckets for s in range(start, start + WINDOW))]
        usual = sorted(history)[len(history) // 2] if len(history) >= 3 else None
        # Sem histórico suficiente não dá para dizer que "explodiu".
        ratio = messages / max(usual, 2) if usual is not None else 0.0
        return {"messages": messages, "ratio": ratio,
                "laughs": laughs / messages if messages else 0.0,
                "hype": hype / messages if messages else 0.0}


class TwitchChat(threading.Thread):
    """Mantém a conexão com o chat de um canal e alimenta o ChatMeter."""

    def __init__(self, channel: str, meter: ChatMeter, host: str = HOST, port: int = PORT,
                 tls: bool = True):
        super().__init__(daemon=True)
        self.channel, self.meter = channel, meter
        self.host, self.port, self.tls = host, port, tls
        self.connected = False
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=15)
        if self.tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        with sock:
            sock.settimeout(5)
            sock.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\n"
                         f"JOIN #{self.channel}\r\n".encode())
            pending, last_data = b"", time.time()
            while not self._halt.is_set():
                try:
                    data = sock.recv(8192)
                except socket.timeout:
                    if time.time() - last_data > 360:   # a Twitch manda PING a cada ~5 min
                        raise ConnectionError("chat mudo demais")
                    continue
                if not data:
                    raise ConnectionError("chat fechou a conexão")
                last_data = time.time()
                *lines, pending = (pending + data).split(b"\r\n")
                for line in lines:
                    text = line.decode("utf-8", "replace")
                    if text.startswith("PING"):
                        sock.sendall(text.replace("PING", "PONG", 1).encode() + b"\r\n")
                    elif " PRIVMSG #" in text:
                        self.connected = True
                        self.meter.add(time.time(), text.split(" :", 1)[-1])
                    elif " JOIN #" in text or " 366 " in text:
                        self.connected = True

    def run(self) -> None:
        wait = 5
        while not self._halt.is_set():
            try:
                self._session()
                wait = 5
            except (OSError, ConnectionError, ssl.SSLError):
                pass
            self.connected = False
            if self._halt.wait(wait):
                break
            wait = min(wait * 2, 120)
