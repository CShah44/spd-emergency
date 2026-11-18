# Build from the repo root:
#   docker build -f services/matching_service/Dockerfile -t matching-service .
FROM python:3.12-slim

WORKDIR /srv
COPY services/matching_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/matching_service/app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Healthy = the HTTP app is up. It deliberately does NOT require RabbitMQ: the
# consumer reconnects by itself, and POST /match/{id} works without the queue.
HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
