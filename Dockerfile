FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

# Run as an unprivileged user; the service never needs root.
RUN useradd --create-home --uid 10001 surgiserve
USER surgiserve

EXPOSE 8000

# python:3.12-slim has no curl, so the health check uses the standard library.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "surgiserve.app:app", "--host", "0.0.0.0", "--port", "8000"]
