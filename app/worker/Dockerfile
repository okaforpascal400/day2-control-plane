# Build context is `app/` so the image can pick up the shared schema package.
#   docker build -f app/worker/Dockerfile app
#
# Base pinned by digest per CLAUDE.md rule 2 — tags move, digests do not.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/shared \
    DAY2_HEARTBEAT_PATH=/tmp/worker-heartbeat

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin day2

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root shared/day2_shared /app/shared/day2_shared
COPY --chown=root:root worker/worker /app/worker

USER 10001

# The worker exposes no port, so liveness is the poll-loop heartbeat: a process
# that is up but no longer polling is exactly the failure worth catching.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys; from worker.config import get_settings; from worker.runner import heartbeat_is_fresh; s = get_settings(); sys.exit(0 if heartbeat_is_fresh(s.heartbeat_path, s.heartbeat_max_age_seconds) else 1)"]

CMD ["python", "-m", "worker.main"]
