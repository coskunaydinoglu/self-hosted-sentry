FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY generator.py ./
RUN useradd --uid 1000 --create-home gen && mkdir -p /data && chown -R gen:gen /srv /data
USER gen
HEALTHCHECK --interval=60s --timeout=5s --start-period=600s --retries=3 \
    CMD ["python", "-c", "import os,time,sys; f='/tmp/health.txt'; sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 3*float(os.environ.get('SESSION_GENERATOR_INTERVAL','600'))+600 else 1)"]
CMD ["python", "generator.py"]
