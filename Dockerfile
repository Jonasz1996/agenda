FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 agenda
COPY agenda ./agenda
COPY tools ./tools
USER agenda
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"
# Eén proces met threads: de dagelijkse recycleapp-sync draait dan één keer (zie sync.py).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "agenda.wsgi:app"]
