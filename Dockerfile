# Backlogr server image. Data (DB, exports, backups) lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BACKLOGR_DB=/data/backlogr.db \
    BACKLOGR_EXPORT=/data/exports/backlogr-export.json \
    BACKLOGR_BACKUP_DIR=/data/backups \
    BACKLOGR_PORT=8796 \
    BACKLOGR_HOSTS=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY cli.py core.py dashboard.py server.py ./
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin backlogr \
 && mkdir -p /data && chown backlogr:backlogr /data && chmod 700 /data

USER backlogr
VOLUME /data
EXPOSE 8796
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"BACKLOGR_PORT\"]}/healthz', timeout=3)"
# HTTP mode needs BACKLOGR_TOKEN; append --stdio for the stdio MCP transport.
ENTRYPOINT ["backlogr-server"]
