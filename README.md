# ViceClips

Transforma lives (ao vivo ou gravadas) em clips verticais 9:16 para Reels, TikTok e Shorts,
no formato dos canais de clips de GTA VI:

```
┌──────────────────┐
│  câmera do       │  facecam detectada automaticamente e recortada
│  streamer        │
├──────────────────┤  faixa rosa
│  Título do Clip  │  título curto do que aconteceu
│                  │
│    gameplay      │  recorte central do jogo
│                  │
└──────────────────┘
```

Identidade visual: preto e rosa (`viceclips/config.py`).

## Instalação

Requer Python 3.10+ e `ffmpeg` no PATH.

```bash
pip install -e ".[ai,dev]"
```

O extra `ai` (título automático e transcrição) é opcional; copie `.env.example` e defina
`ANTHROPIC_API_KEY` para usá-lo. Sem ele, passe `--title`.

## Interface web

```bash
viceclips web
```

Abra http://127.0.0.1:8000. O menu lateral tem quatro telas:

- **Novo clip**: cole o link de uma live gravada (por intervalo ou automático) ou envie um arquivo.
- **Ao vivo**: cole o link da live; o sistema guarda os últimos 10 minutos e você clipa na hora.
- **Fila**: andamento de cada clip (um render por vez).
- **Meus clips**: assistir, baixar e excluir os clips prontos.

O servidor escuta só na própria máquina e não tem login; não o exponha na internet como está.

## Uso pela linha de comando

```bash
# trecho local já recortado no tempo
viceclips render trecho.mp4 --title "GTA 6 Alien Abduction" --credit "@canal"

# live gravada: intervalo manual
viceclips vod URL --start 1:23:45 --end 1:24:30

# live gravada: 5 melhores momentos por pico de áudio
viceclips vod URL --auto 5

# live ao vivo: deixa o buffer rodando...
viceclips live URL
# ...e, quando acontecer algo, em outro terminal:
viceclips live-clip --last 45
```

Se a detecção errar a câmera, informe a posição no vídeo de origem: `--cam X,Y,L,A`.
Os clips saem em `out/`; arquivos intermediários ficam em `work/`.

## Estrutura

```
viceclips/
  cli.py               comandos: web, render, vod, live, live-clip
  web/app.py           API (FastAPI) e fila de jobs
  web/static/          página única: menu lateral, formulários, galeria
  pipeline.py          detectar câmera -> título -> render
  config.py            layout 1080x1920, cores, fontes
  media.py             helpers de ffmpeg/ffprobe
  ingest/vod.py        download de trechos de lives gravadas (yt-dlp)
  ingest/live.py       buffer circular da live + corte dos últimos N segundos
  detect/webcam.py     localização da facecam (rosto estável + bordas fixas)
  highlights/audio.py  candidatos a clip por pico de áudio
  ai/transcribe.py     transcrição da fala (faster-whisper)
  ai/title.py          título curto a partir de frames + transcrição (Claude)
  render/title_card.py overlay PNG: título, faixa rosa, crédito
  render/compose.py    montagem final com ffmpeg
tests/
docs/PLANO.md          plano de fases
```

## Testes

```bash
pytest
```

Plano completo e próximos passos: [docs/PLANO.md](docs/PLANO.md).
