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

- **Novo clip**: cole o link de uma live gravada. No modo "Melhores momentos" (padrão) o sistema
  varre a live inteira e entrega um clip para cada momento em que o streamer mais reage; no modo
  "Por intervalo" você informa início e fim. Também aceita um arquivo de vídeo.
- **Ao vivo**: cole o link da live. O sistema clipa sozinho quando o áudio dispara (grito, risada,
  susto) e guarda os últimos 10 minutos para você clipar na hora com um botão.
- **Fila**: andamento de cada clip (um render por vez).
- **Meus clips**: assistir, baixar e excluir os clips prontos.

A fila fica gravada em `work/jobs.json`: se o servidor reiniciar, os clips que estavam em
andamento são retomados sozinhos.

### Acesso de outros aparelhos (com senha)

Por padrão o site só atende na própria máquina. Para abrir pelo celular ou hospedar, defina
uma senha; sem ela o servidor se recusa a subir fora de `127.0.0.1`.

```bash
VICECLIPS_PASSWORD="sua-senha" viceclips web --host 0.0.0.0
```

Na internet, coloque o site atrás de HTTPS (Caddy, nginx, Cloudflare Tunnel); sem isso a
senha trafega em texto puro.

## App no celular

O site é um app instalável (PWA): depois de hospedado com HTTPS, abra o endereço no celular e

- **Android (Chrome)**: menu ⋮ → "Instalar app" (ou "Adicionar à tela inicial").
- **iPhone (Safari)**: botão Compartilhar → "Adicionar à Tela de Início".

Ele abre em tela cheia, com ícone próprio, e faz tudo o que o site faz. No Android, o ViceClips
também aparece no menu "Compartilhar" dos outros apps: compartilhe o link de uma live da Twitch
ou do YouTube e ele já abre com o link preenchido.

## Hospedagem no Coolify

O repositório traz `Dockerfile` e `docker-compose.yml`.

1. No Coolify: **New Resource → Public/Private Repository**, aponte para este repositório e
   escolha o build pack **Dockerfile** (ou **Docker Compose**).
2. Porta exposta: `8000`.
3. Variáveis de ambiente:
   - `VICECLIPS_PASSWORD` (obrigatória): senha do site. Sem ela o servidor não sobe.
   - `ANTHROPIC_API_KEY` (opcional): liga o título automático. Sem ela os clips saem sem título.
4. **Persistent Storage**: monte um volume em `/data` (clips prontos e fila). Sem volume, os
   clips somem a cada deploy.
5. Configure o domínio; o Coolify emite o HTTPS, que é necessário para instalar o app.

Vídeo consome CPU e disco: um render usa um núcleo inteiro por alguns segundos a minutos, e a
live ao vivo mantém até ~10 minutos de buffer em disco.

Para testar a imagem localmente:

```bash
docker build -t viceclips .
```

```bash
docker run -p 8000:8000 -e VICECLIPS_PASSWORD=troque-esta-senha -v viceclips-data:/data viceclips
```

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
  highlights/audio.py  candidatos a clip por pico de áudio (live gravada)
  highlights/live_watch.py  clip automático na live ao vivo
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
