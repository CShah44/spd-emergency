# The Delta Command web page. Build from the repo root:
#   docker build -f services/ui_service/Dockerfile -t ui-service .
#
# Two stages: Node builds the Svelte page into plain HTML, CSS, JS and fonts; the final image
# is Python only (FastAPI serves those files and the live API), so no Node at run time.

FROM node:22-alpine AS page
WORKDIR /web
COPY services/ui_service/web/package.json services/ui_service/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/ui_service/web ./
RUN npm run build -- --outDir /page --emptyOutDir

FROM python:3.12-slim
WORKDIR /srv
COPY services/ui_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/ui_service/app ./app
COPY --from=page /page ./app/static

ENV PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"

# exec: uvicorn becomes PID 1 so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
