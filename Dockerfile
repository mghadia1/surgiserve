# Two images from one file: `api` (also used by the one-shot migrate step and
# without PyTorch) and `worker` (with CPU-only PyTorch and the model code).
# Compose picks the target per service.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as an unprivileged user; neither service needs root. The storage
# directory is created here, owned by that user, because a new named volume
# copies the ownership of the directory it is mounted over.
RUN useradd --create-home --uid 10001 surgiserve \
    && mkdir -p /data/storage \
    && chown surgiserve:surgiserve /data/storage


FROM base AS api

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations

USER surgiserve
EXPOSE 8000

# Liveness only (GET /health); see src/surgiserve/health.py for why.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "surgiserve.healthcheck", "api"]

# Sets up JSON logging, then runs uvicorn (see src/surgiserve/serve.py).
CMD ["python", "-m", "surgiserve.serve"]


FROM base AS worker

# CPU-only PyTorch, in its own layer before the code, so code changes do not
# re-download it. From PyPI, x86 Linux would get the multi-GB CUDA build.
RUN python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu torch==2.13.0

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[model]'

USER surgiserve

# Healthy while the main loop keeps touching its liveness file. The start
# period covers loading torch and the model.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "surgiserve.healthcheck", "worker"]

CMD ["python", "-m", "surgiserve.worker"]
