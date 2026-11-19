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


# ---------- segunda etapa: olhar o candidato ----------

def _video(tmp_path, name: str, source: str) -> str:
    import subprocess
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-t", "4",
                    "-c:v", "libx264", "-preset", "ultrafast", str(path)], check=True)
    return str(path)


def test_rerank_tells_a_static_screen_from_gameplay(tmp_path):
    from viceclips.highlights import rerank

    paused = rerank.visual(_video(tmp_path, "pausa.mp4", "color=c=gray:s=640x360:r=30"), None)
    playing = rerank.visual(_video(tmp_path, "jogo.mp4", "testsrc2=s=640x360:r=30"), None)
    assert paused["static"] is True and playing["static"] is False
    assert playing["game_motion"] > paused["game_motion"] + 1


def test_rerank_measures_how_lively_the_streamer_is_on_camera(tmp_path):
    from viceclips.config import Rect
    from viceclips.highlights import rerank

    cam = Rect(0, 0, 160, 120)
    # câmera parada sobre jogo em movimento x câmera agitada
    calm = _video(tmp_path, "calmo.mp4", "testsrc2=s=640x360:r=30,drawbox=0:0:160:120:gray:fill")
    lively = _video(tmp_path, "agitado.mp4", "testsrc2=s=640x360:r=30")
    assert rerank.visual(calm, cam)["reaction"] == 0.0
    assert rerank.visual(lively, cam)["reaction"] > 0.5


def test_rerank_speech_counts_reaction_words():
    from viceclips.ai.transcribe import Word
    from viceclips.highlights import rerank

    say = lambda text: [Word(i, i + 0.3, w) for i, w in enumerate(text.split())]
    assert rerank.speech(say("então a gente vai ali pegar o carro")) == 0.0
    assert rerank.speech(say("meu deus que isso mano não acredito")) == 1.0
    assert 0 < rerank.speech(say("nossa olha o tamanho disso")) < 1


def test_rerank_prefers_a_real_reaction_over_a_louder_static_screen():
    from viceclips.highlights import rerank

    loud_break = rerank.final_score(30, {"static": True, "reaction": 0.0}, 0.0, False)       # "já volto" com música
    quiet_reaction = rerank.final_score(8, {"static": False, "reaction": 0.7}, 0.67, True)   # streamer reagindo
    plain_gameplay = rerank.final_score(8, {"static": False, "reaction": 0.0}, 0.0, False)
    assert quiet_reaction > plain_gameplay > loud_break


# ---------- quantidade de clips por hora ----------

def test_rate_controller_keeps_only_the_strongest_moments_per_hour():
    from viceclips.highlights.rate import RateController

    spread = np.random.default_rng(3)
    control, accepted = RateController(per_hour=4), []
    for t in range(0, 4 * 3600, 60):                  # um momento por minuto durante 4 horas
        strength = float(spread.uniform(6, 30))
        if control.accepts(t, strength):
            accepted.append((t, strength))
        control.observe(t, strength)
    later = [s for t, s in accepted if t > 3600]      # depois do aprendizado
    assert 6 <= len(later) <= 20                      # perto de 4 por hora em 3 horas, não 60 por hora
    assert min(later) > 22                            # e são os fortes, não os primeiros que apareceram


def test_rate_controller_adapts_to_a_quiet_and_to_a_loud_stream():
    from viceclips.highlights.rate import FLOOR, RateController

    quiet, loud = RateController(4), RateController(4)
    for t in range(0, 3600, 1300):                    # live calma: 3 momentos na hora (menos que os 4 pedidos)
        quiet.observe(t, 9.0)
    for t in range(0, 3600, 50):                      # live barulhenta: momento forte o tempo todo
        loud.observe(t, 20.0 + (t % 7))
    assert quiet.threshold(3600) == FLOOR             # quase tudo que se destacar entra
    assert loud.threshold(3600) > 22                  # a régua sobe junto com a live


def test_rate_controller_merges_a_burst_of_peaks_and_chat_lowers_the_bar():
    from viceclips.highlights.rate import INITIAL, RateController

    control = RateController(4)
    for t in (100, 110, 120, 130):                    # mesma gritaria
        control.observe(t, 18.0 + t / 100)
    assert len(control.events) == 1 and control.events[0][1] == 19.3
    assert not control.accepts(200, INITIAL - 2)
    assert control.accepts(200, INITIAL - 2, chat_reacted=True)
