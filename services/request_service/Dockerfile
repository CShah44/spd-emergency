# Build from the repo root:
#   docker build -f services/request_service/Dockerfile -t request-service .
FROM python:3.12-slim

WORKDIR /srv
COPY services/request_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/request_service/app ./app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"

# exec: uvicorn becomes PID 1 so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
