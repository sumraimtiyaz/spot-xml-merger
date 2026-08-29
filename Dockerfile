FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=production
WORKDIR /srv

# lxml needs no build tools on slim for manylinux wheels, but keep curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY app ./app
COPY wsgi.py .

RUN useradd -m -u 10001 app && chown -R app:app /srv
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# 2 workers is plenty: a merge is milliseconds of CPU and holds no state.
CMD ["gunicorn","--bind","0.0.0.0:8000","--workers","2","--threads","4",\
     "--timeout","60","--access-logfile","-","--error-logfile","-",\
     "--access-logformat","%(h)s %(m)s %(U)s %(s)s %(M)sms","wsgi:app"]
