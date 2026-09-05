FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY canary.py ./
RUN useradd --uid 1000 --create-home canary && chown -R canary:canary /srv
USER canary

# Healthy while the loop keeps completing cycles: the health file must be fresher than 3 intervals.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,time,sys; f=os.environ.get('INGEST_CANARY_HEALTH_FILE','/tmp/health.txt'); i=float(os.environ.get('INGEST_CANARY_INTERVAL','300')); sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 3*i+300 else 1)"]

CMD ["python", "canary.py"]
