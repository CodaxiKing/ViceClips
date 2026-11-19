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

- [ ] Pico de mensagens no chat (Twitch IRC / chat replay do YouTube)
- [ ] Palavras-gatilho na transcrição ("no way", "meu deus", risadas)
- [ ] Na live ao vivo: disparo automático do `live-clip` quando o score passar do limiar
- [ ] IA para ranquear os candidatos e ajustar início e fim do corte

### Fase 4: acabamento do vídeo

- [ ] Legendas palavra a palavra com destaque em rosa (como o "DOG" do exemplo)
- [ ] Emoji no título (fonte colorida separada)
- [ ] Intro/outro e marca d'água do canal
- [ ] Reenquadramento dinâmico do gameplay seguindo a ação

### Fase 5: produto

- [ ] API + fila de jobs (FastAPI + worker) e painel web: colar link, revisar, aprovar
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
