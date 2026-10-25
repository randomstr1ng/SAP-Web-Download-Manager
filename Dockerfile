# syntax=docker/dockerfile:1

# ---- Stage 1: builder -------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

COPY pyproject.toml ./
COPY sapdlm ./sapdlm
COPY web ./web
COPY ui5 ./ui5

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# ---- Stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DLM_BIND=0.0.0.0:8000 \
    DLM_DOWNLOAD_DIR=/data/downloads \
    DLM_DATA_DIR=/data/app \
    DLM_WEB_DIR=/app/web \
    DLM_UI5_DIR=/app/ui5

# Non-root user (uid/gid 1000), volumes mounted writable below
RUN groupadd -g 1000 sapdlm && useradd -u 1000 -g sapdlm -m sapdlm \
    && mkdir -p /data/downloads /data/app \
    && chown -R sapdlm:sapdlm /data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/web /app/web
COPY --from=builder /build/ui5 /app/ui5
# Ensure the runtime user can read UI assets regardless of host file modes.
RUN chmod -R a+rX /app/web /app/ui5

WORKDIR /app
USER sapdlm

EXPOSE 8000
VOLUME ["/data/downloads", "/data/app"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /opt/venv/bin/python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["python", "-m", "sapdlm.main"]
