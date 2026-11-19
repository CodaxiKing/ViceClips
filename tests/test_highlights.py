"""Análise de momentos: áudio sintético com fala calma, um susto e uma risada."""
import socket
import threading
import time

import numpy as np

from viceclips.highlights import analysis
from viceclips.highlights.analysis import FUNNY, IMPACT, RATE
from viceclips.highlights.chat import ChatMeter, TwitchChat, twitch_channel
from viceclips.highlights.live_watch import LiveWatcher

rng = np.random.default_rng(7)


def speech(seconds: int, volume: float = 300) -> np.ndarray:
    """Fala calma: ruído grave com pausas curtas."""
    t = np.arange(seconds * RATE) / RATE
    voice = np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 360 * t)
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t)
    return (voice * envelope * volume + rng.normal(0, 20, len(t))).astype(np.float32)


def scream(seconds: int) -> np.ndarray:
    """Grito: alto, agudo e contínuo."""
    t = np.arange(seconds * RATE) / RATE
    return (np.sin(2 * np.pi * 2200 * t) * 9000 + rng.normal(0, 800, len(t))).astype(np.float32)


def laughter(seconds: int) -> np.ndarray:
    """Risada: rajadas agudas ("ha-ha-ha") a ~5 por segundo."""
    t = np.arange(seconds * RATE) / RATE
    bursts = (np.sin(2 * np.pi * 5 * t) > 0.2).astype(np.float32)
    return (np.sin(2 * np.pi * 1800 * t) * 4000 * bursts + rng.normal(0, 60, len(t))).astype(np.float32)


def stream_with(events: dict[int, np.ndarray], seconds: int = 300) -> np.ndarray:
    pcm = speech(seconds)
    for at, sound in events.items():
        pcm[at * RATE: at * RATE + len(sound)] = sound
    return pcm


def test_calm_speech_has_no_moments():
    assert analysis.pick_moments(analysis.features_from_pcm(speech(300)), count=5) == []


def test_steady_loud_stream_has_no_moments():
    assert analysis.pick_moments(analysis.features_from_pcm(speech(300, volume=6000)), count=5) == []


def test_finds_scream_and_laughter_and_tells_them_apart():
    feats = analysis.features_from_pcm(stream_with({100: scream(2), 200: laughter(6)}))
    moments = analysis.pick_moments(feats, count=5)
    assert [(round(m.peak / 10) * 10, m.kind) for m in moments] == [(100, IMPACT), (200, FUNNY)]
    for m in moments:
        assert 12 <= m.peak - m.start <= 25          # contexto antes do momento
        assert 5 <= m.end - m.peak <= 15             # termina quando a reação esfria
        assert m.end - m.start <= 41


def test_count_limits_and_keeps_the_strongest():
    feats = analysis.features_from_pcm(stream_with({80: laughter(3), 160: scream(3), 240: laughter(6)}))
    assert len(analysis.pick_moments(feats, count=5)) == 3
    assert len(analysis.pick_moments(feats, count=1)) == 1


def feed_live(watcher: LiveWatcher, pcm: np.ndarray, chunk: int = 6) -> None:
    for start in range(0, len(pcm) // RATE, chunk):
        watcher.feed(analysis.features_from_pcm(pcm[start * RATE: (start + chunk) * RATE]))
        watcher.evaluate()


def test_live_watcher_clips_each_moment_once():
    found = []
    watcher = LiveWatcher(None, lambda seconds, kind, score: found.append((watcher.total, seconds, kind)))
    feed_live(watcher, stream_with({100: scream(2), 104: scream(1), 200: laughter(6)}))
    assert [kind for _, _, kind in found] == [IMPACT, FUNNY]
    for now, seconds, _ in found:
        assert 17 <= seconds <= 45                   # clip cobre o antes e o depois
    assert 105 <= found[0][0] <= 125                 # cortou pouco depois do susto, não minutos depois


def test_live_watcher_stays_quiet_on_calm_stream():
    found = []
    watcher = LiveWatcher(None, lambda *args: found.append(args))
    feed_live(watcher, speech(300))
    assert found == []


def test_chat_burst_of_laughter_triggers_a_funny_clip(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    meter, found = ChatMeter(), []
    watcher = LiveWatcher(None, lambda seconds, kind, score: found.append(kind), chat=meter)
    pcm = speech(240)
    for start in range(0, 240, 6):
        clock[0] = 1000.0 + start + 6
        for second in range(start, start + 6):       # chat normal: 1 mensagem a cada 2 s
            if second % 2 == 0:
                meter.add(1000.0 + second, "boa noite chat")
            if 150 <= second < 158:                  # explosão de risada
                for _ in range(6):
                    meter.add(1000.0 + second, "KKKKKKKKKK")
        watcher.feed(analysis.features_from_pcm(pcm[start * RATE: (start + 6) * RATE]))
        watcher.evaluate()
    assert found == [FUNNY]


def test_chat_meter_needs_history_and_ignores_plain_fast_chat():
    meter = ChatMeter()
    for i in range(20):
        meter.add(500.0 + i * 0.1, "KKKKK")
    assert meter.burst(500.0)["ratio"] == 0.0        # sem histórico não há "explosão"
    assert meter.burst(500.0)["laughs"] == 1.0


def test_twitch_channel_from_url():
    assert twitch_channel("https://m.twitch.tv/alanzoka?desktop-redirect=true") == "alanzoka"
    assert twitch_channel("https://www.twitch.tv/Canal_1") == "canal_1"
    assert twitch_channel("https://www.twitch.tv/videos/123456") is None
    assert twitch_channel("https://www.youtube.com/watch?v=abc") is None


def test_twitch_chat_client_joins_answers_ping_and_reads_messages():
    server = socket.create_server(("127.0.0.1", 0))
    port, seen = server.getsockname()[1], {}

    def fake_twitch():
        conn, _ = server.accept()
        with conn:
            seen["hello"] = conn.recv(1024).decode()
            conn.sendall(b":tmi.twitch.tv 366 justinfan #canal :End of /NAMES list\r\n"
                         b"PING :tmi.twitch.tv\r\n")
            seen["pong"] = conn.recv(1024).decode()
            conn.sendall(b":ana!ana@ana.tmi.twitch.tv PRIVMSG #canal :KKKKKK que isso\r\n"
                         b":bia!bia@bia.tmi.twitch.tv PRIVMSG #canal :oi\r\n")
            time.sleep(0.5)

    threading.Thread(target=fake_twitch, daemon=True).start()
    meter = ChatMeter()
    chat = TwitchChat("canal", meter, host="127.0.0.1", port=port, tls=False)
    chat.start()
    deadline = time.time() + 5
    while time.time() < deadline and sum(b[0] for b in meter._buckets.values()) < 2:
        time.sleep(0.05)
    connected = chat.connected
    chat.stop()
    server.close()

    assert "JOIN #canal" in seen["hello"] and seen["hello"].startswith("NICK justinfan")
    assert seen["pong"].startswith("PONG :tmi.twitch.tv")
    assert connected
    totals = [sum(col) for col in zip(*meter._buckets.values())]
    assert totals[0] == 2 and totals[1] == 1         # 2 mensagens, 1 risada
