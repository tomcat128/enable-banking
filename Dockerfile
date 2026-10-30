FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /app/src

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data
USER app

ENV EB_DATA_DIR=/data \
    EB_PRIVATE_KEY_PATH=/secrets/private.pem
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["enable-banking"]
CMD ["run"]
