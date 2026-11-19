FROM python:3.12-slim

# ffmpeg faz todo o processamento de vídeo; as fontes são usadas no título dos clips.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-inter fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY viceclips ./viceclips
RUN pip install --no-cache-dir . anthropic

# Clips prontos e fila ficam em /data: monte um volume persistente aqui.
ENV VICECLIPS_WORK=/data/work \
    VICECLIPS_OUT=/data/out \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/api/session || exit 1

# Exige VICECLIPS_PASSWORD no ambiente; sem ela o servidor se recusa a subir.
CMD ["viceclips", "web", "--host", "0.0.0.0", "--port", "8000"]
