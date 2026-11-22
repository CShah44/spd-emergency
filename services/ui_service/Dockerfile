# The Delta Command web page. Build from the repo root:
#   docker build -f services/ui_service/Dockerfile -t ui-service .
FROM python:3.12-slim

WORKDIR /srv
COPY services/ui_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/ui_service/app ./app

ENV PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"

# exec: python becomes PID 1 so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec python -m app.main"]
