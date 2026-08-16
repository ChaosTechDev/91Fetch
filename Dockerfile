FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIEWKEY_HOST=0.0.0.0 \
    VIEWKEY_PORT=8765 \
    VIEWKEY_DATA_DIR=/data \
    VIEWKEY_DEFAULT_DOWNLOAD_DIR=/data/videos \
    VIEWKEY_NO_BROWSER=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY site.example.json ./site.json
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /data/videos
EXPOSE 8765
VOLUME ["/data"]
CMD ["python", "-m", "viewkey_batch.web"]
