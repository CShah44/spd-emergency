# Build from the repo root:
#   docker build -f services/storage_node/Dockerfile -t storage-node .
FROM python:3.12-slim

WORKDIR /srv
COPY services/storage_node/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/storage_node/app ./app

# The SQLite file lives here: mount a volume so data survives restarts.
ENV DATA_DIR=/data \
    PORT=9000 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 9000

HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/health', timeout=2)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port $PORT"]
