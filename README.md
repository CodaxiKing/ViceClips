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
- **Ao vivo**: acompanhe até 4 lives ao mesmo tempo. O sistema clipa sozinho quando o streamer
  reage forte e guarda os últimos 30 minutos de cada uma para você voltar atrás e clipar. Em
  "Canais acompanhados" você cadastra canais e ele começa a gravar quando entram ao vivo.
- **Fila**: andamento de cada clip (um render por vez).
- **Publicar**: contas conectadas e posts agendados (ver "Publicação").
- **Servidor**: disco, processador, memória, fila, tempo por clip e erros recentes.
- **Meus clips**: assistir, baixar (um ou todos em .zip), favoritar (★ não é apagado),
  avaliar (👍/👎), publicar e excluir. Cada clip é apagado sozinho
  1 hora depois de pronto; as avaliações ficam guardadas em `work/ratings.jsonl`.

Os clips saem com legenda palavra por palavra (a palavra falada fica em rosa), feita por
transcrição local, sem API. Só a fala do streamer é legendada: o diálogo do jogo, em
inglês, é descartado pelo idioma. O botão "Ativar avisos" no menu manda uma notificação para o
celular quando um clip fica pronto.

| Variável | Padrão | Para quê |
|---|---|---|
| `VICECLIPS_CLIP_TTL_MINUTES` | `60` | minutos até apagar um clip pronto (`0` = nunca) |
| `VICECLIPS_CAPTIONS` | `1` | `0` desliga as legendas |
| `VICECLIPS_CAPTION_LANG` | `pt` | idioma do streamer; só fala nesse idioma vira legenda (vazio = legenda tudo) |
| `VICECLIPS_WHISPER_MODEL` | `small` | modelo de transcrição (`tiny`, `base`, `small`, `medium`) |
| `VICECLIPS_MAX_LIVES` | `4` | lives acompanhadas ao mesmo tempo (cada uma sem limite de tempo) |
| `VICECLIPS_BUFFER_MINUTES` | `30` | minutos de cada live guardados para voltar atrás (2 a 90) |
| `VICECLIPS_LOGO_OPACITY` | `0.7` | opacidade da logo do canal nos clips (`0` tira a logo) |

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

## Publicação

Na tela **Publicar** você conecta as contas uma vez; depois, cada clip tem um botão "Publicar"
para postar na hora ou agendar. Cada plataforma exige um app de desenvolvedor seu, e as chaves
entram como variáveis de ambiente no servidor. O endereço de retorno (redirect) de cada uma
aparece na própria tela Publicar.

| Plataforma | Onde criar o app | Variáveis | Atenção |
|---|---|---|---|
| YouTube Shorts | console.cloud.google.com → ative "YouTube Data API v3" → credenciais OAuth "Aplicativo da Web" | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` | Projeto não verificado pelo Google: os vídeos sobem como **privados** até você pedir a auditoria da API. |
| TikTok | developers.tiktok.com → app com "Content Posting API" e escopo `video.publish` | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | App não auditado pelo TikTok: os posts saem **só para você** (privados) até a auditoria. |
| Instagram Reels | developers.facebook.com → app com "Instagram API with Instagram Login" | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` | Precisa de conta **profissional** (Criador ou Empresa). Em modo de desenvolvimento funciona para as contas cadastradas como testadoras do app. |

Um clip com post agendado não é apagado pela faxina automática; depois de publicado, volta
a valer o prazo normal. Os conectores foram testados contra um servidor que imita as APIs, não
contra as plataformas reais: a primeira publicação de verdade é o teste final.

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
live ao vivo guarda os últimos 30 minutos em disco (uns 60 MB por minuto em 1080p60; ajuste com
`VICECLIPS_BUFFER_MINUTES`).

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
  web/app.py           API (FastAPI), fila de jobs, validade dos clips, avaliações
  web/lives.py         várias lives ao mesmo tempo e canais acompanhados
  web/push.py          avisos no celular (Web Push)
  web/publish.py       contas, agendamento e envio para YouTube, TikTok e Instagram
  web/static/          página única: menu lateral, formulários, galeria
  pipeline.py          detectar câmera -> título -> render
  config.py            layout 1080x1920, cores, fontes
  media.py             helpers de ffmpeg/ffprobe
  ingest/vod.py        trechos de lives gravadas (HLS direto; yt-dlp nos demais sites)
  ingest/hls.py        download de segmentos HLS por HTTP
  ingest/live.py       buffer circular da live + corte dos últimos N segundos
  detect/webcam.py     localização da facecam (rosto estável + bordas fixas; modelo YuNet)
  highlights/analysis.py  análise do áudio: nível, agudos e rajadas vs. o normal da live
  highlights/chat.py   chat da Twitch ao vivo como sinal (leitura anônima)
  highlights/calibration.py  ajuste da exigência pelas avaliações 👍/👎
  highlights/audio.py  melhores momentos de uma live gravada (1ª etapa: áudio)
  highlights/rerank.py 2ª etapa: nota de cada candidato pelo que ele mostra
  highlights/live_watch.py  clip automático na live ao vivo
  ai/transcribe.py     transcrição local da fala, com tempo por palavra (faster-whisper)
  ai/title.py          título curto a partir de frames + transcrição (Claude)
  render/title_card.py overlay PNG: título, faixa rosa, crédito
  render/captions.py   legendas palavra por palavra (ASS)
  render/compose.py    montagem final com ffmpeg
tests/
docs/PLANO.md          plano de fases
```

## Testes

```bash
pytest
```

Plano completo e próximos passos: [docs/PLANO.md](docs/PLANO.md).
