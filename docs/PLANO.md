# Plano do ViceClips

Objetivo: colar o link de uma live (ao vivo ou gravada) e receber clips verticais prontos
para postar, com a câmera do streamer em cima, o gameplay embaixo e um título curto do
que aconteceu, na identidade preto e rosa.

## Fluxo

```
link ──> ingestão ──> seleção do momento ──> detecção da câmera ──> título ──> render 9:16 ──> out/
         vod / live    manual ou highlights   rosto + bordas         IA         ffmpeg
```

1. **Ingestão.** Live gravada: `yt-dlp` baixa só o intervalo pedido. Live ao vivo: `ffmpeg`
   grava um buffer circular de ~10 min em segmentos; o clip é cortado do final do buffer.
2. **Seleção do momento.** Manual (`--start/--end`, `--last N`) ou automática por pico de áudio.
3. **Detecção da câmera.** Rosto que fica parado na maioria dos frames amostrados = facecam;
   o retângulo é ajustado às bordas fixas do overlay. Sem câmera, o gameplay ocupa a tela toda.
4. **Título.** Frames do clip + transcrição vão para o Claude, que devolve até 5 palavras.
5. **Render.** `ffmpeg` recorta câmera e gameplay, empilha, e sobrepõe o PNG do título.

## Fases

### Fase 1: núcleo (feito neste scaffold)

- [x] Render 9:16 com câmera em cima, faixa rosa, título e crédito
- [x] Detecção automática da câmera, com `--cam` manual e fallback sem câmera
- [x] Live gravada por intervalo e por highlights de áudio
- [x] Live ao vivo com buffer circular e `live-clip --last N`
- [x] Título automático por IA (opcional)

### Fase 2: calibrar com lives reais

- [ ] Montar um conjunto de 20 a 30 trechos de streamers diferentes e medir o acerto da
      detecção de câmera; ajustar limiares ou trocar o detector (YuNet / MediaPipe)
- [ ] Câmera sem moldura ou com chroma key: segmentar a pessoa em vez de procurar bordas
- [ ] Guardar a posição da câmera por canal, para não redetectar a cada clip
- [ ] Validar download de trechos em Twitch, YouTube e Kick

### Fase 3: highlights melhores

Como a análise decide hoje (`highlights/analysis.py`, `chat.py`, `live_watch.py`):

- Por segundo de áudio mede nível, agudos (grito/risada) e rajadas, sempre em relação ao
  "normal" dos 3 minutos anteriores da própria live.
- Um momento precisa ficar 10 dB acima do normal (6 dB se o chat também reagir).
- O corte começa numa pausa da fala 12 a 25 s antes e termina quando a reação esfria.
- Ao vivo, o chat da Twitch (leitura anônima) reforça a nota, decide entre "engraçado" e
  "impactante" e dispara clip sozinho quando explode de risada.
- Ao vivo há um teto de 8 clips automáticos por meia hora; passando da metade, só entram
  os que superam a mediana dos anteriores.

- [x] Análise multi-sinal do áudio com cortes ajustados ao momento
- [x] Pico de mensagens no chat da Twitch ao vivo

- [ ] Chat de lives gravadas e de YouTube/Kick (a Twitch não expõe mais o replay pelo yt-dlp)
- [ ] Palavras-gatilho na transcrição ("no way", "meu deus", risadas)
- [x] Na live ao vivo: clip automático quando o áudio dispara (interface web)
- [ ] IA para entender o conteúdo (frames + transcrição) e descartar falsos positivos;
      é o que falta para separar "engraçado" de "impactante" com confiança sem chat

### Fase 4: acabamento do vídeo

- [ ] Legendas palavra a palavra com destaque em rosa (como o "DOG" do exemplo)
- [ ] Emoji no título (fonte colorida separada)
- [ ] Intro/outro e marca d'água do canal
- [ ] Reenquadramento dinâmico do gameplay seguindo a ação

### Fase 5: produto

- [x] Interface web com menu lateral: novo clip, ao vivo, fila e galeria (`viceclips web`)
- [x] Fila persistente: jobs gravados em disco e retomados após reinício
- [x] Login por senha (`VICECLIPS_PASSWORD`), exigido para atender fora da própria máquina
- [x] App de celular instalável (PWA) com "Compartilhar para o ViceClips" no Android
- [x] Deploy por Docker (Coolify): `Dockerfile` e `docker-compose.yml`
- [ ] Contas por usuário (hoje é uma senha única)
- [ ] Notificação no celular quando um clip fica pronto
- [ ] App nas lojas (Play Store / App Store), se o PWA não bastar
- [ ] Editor de ajuste fino: arrastar o retângulo da câmera, editar título, aparar corte
- [ ] Publicação agendada em Reels, TikTok e Shorts
- [ ] Vários canais monitorados ao mesmo tempo

## Decisões

- **Python + ffmpeg + OpenCV.** Todo o processamento pesado é do ffmpeg; Python só orquestra.
- **OpenCV 4.x.** A série 5 removeu o `CascadeClassifier` usado na detecção de rosto.
- **Título como PNG (Pillow).** Dá controle de fonte, contorno e quebra de linha sem depender
  do `drawtext` do ffmpeg.
- **Highlights como lista de `(início, fim, score)`.** Novos sinais (chat, transcrição) entram
  somando score, sem mudar o resto do pipeline.

## Cuidados

- **Direitos e regras das plataformas.** Clips de terceiros pedem autorização ou, no mínimo,
  crédito ao streamer (`--credit`); Twitch, YouTube e Kick têm regras próprias sobre download
  e repostagem.
- **Marca.** O projeto usa só a paleta preto e rosa; o logo oficial de GTA VI é marca da
  Rockstar e não deve entrar nos vídeos nem na identidade do canal.
