FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --create-home appuser

COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser data ./data

USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=4)"

# The store starts empty on purpose; set BASEDRIFT_SEED_DEMO=1 for the demo dashboard.
CMD ["uvicorn", "webhook_app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
