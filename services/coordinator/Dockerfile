# Build from the repo root:
#   docker build -f services/coordinator/Dockerfile -t coordinator .
# Runs the real coordinator. For the in-memory fake: -e APP_MODULE=app.fake:app
FROM python:3.12-slim

WORKDIR /srv
COPY services/coordinator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/coordinator/app ./app

ENV APP_MODULE=app.main:app \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn $APP_MODULE --host 0.0.0.0 --port 8000"]
