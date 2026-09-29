FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

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

# python:3.12-slim has no curl, so the health check uses the standard library.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "surgiserve.app:app", "--host", "0.0.0.0", "--port", "8000"]
