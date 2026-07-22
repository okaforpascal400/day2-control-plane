# Build context is `app/` so the image can pick up the shared schema package.
#   docker build -f app/api/Dockerfile app
#
# Base pinned by digest per CLAUDE.md rule 2 — tags move, digests do not.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# asyncpg ships manylinux wheels; the toolchain is only a fallback for arches
# without one, and it never reaches the runtime stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY api/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/shared

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin day2

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root shared/day2_shared /app/shared/day2_shared
COPY --chown=root:root api/api /app/api

USER 10001

EXPOSE 8000

# Liveness only: /health never touches Postgres, so a DB blip cannot restart us.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
