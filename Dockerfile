FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# CPU-only PyTorch, in its own layer so code changes do not re-download it.
# From PyPI, x86 Linux would get the multi-GB CUDA build instead.
RUN python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu torch==2.13.0

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations

# Run as an unprivileged user; the service never needs root. The storage
# directory is created here, owned by that user, because a new named volume
# copies the ownership of the directory it is mounted over.
RUN useradd --create-home --uid 10001 surgiserve \
    && mkdir -p /data/storage \
    && chown surgiserve:surgiserve /data/storage
USER surgiserve

EXPOSE 8000

# Liveness only (GET /health); see src/surgiserve/health.py for why. The worker
# service overrides this with its own check in compose.yaml.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "surgiserve.healthcheck", "api"]

# Sets up JSON logging, then runs uvicorn (see src/surgiserve/serve.py).
CMD ["python", "-m", "surgiserve.serve"]
