FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    B81_TOKEN_CACHE=/data/token.json \
    B81_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY beat81/ ./beat81/

RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"B81_PORT\",\"8000\")}/healthz').read()" || exit 1

CMD ["sh", "-c", "exec uvicorn beat81.server:app --host 0.0.0.0 --port ${B81_PORT:-8000}"]
