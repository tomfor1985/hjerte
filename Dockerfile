FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN groupadd --gid 10001 hjerte && useradd --uid 10001 --gid 10001 --create-home hjerte
COPY --chown=hjerte:hjerte . ./
RUN mkdir -p /app/data /app/backups /app/staticfiles && chown -R hjerte:hjerte /app/data /app/backups /app/staticfiles && chmod +x /app/bin/start.sh
USER hjerte
EXPOSE 8000
CMD ["/app/bin/start.sh", "web"]
