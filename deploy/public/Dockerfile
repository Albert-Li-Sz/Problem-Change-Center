FROM python:3.14-slim-trixie AS api
WORKDIR /app
COPY backend/requirements-public.lock /app/requirements-public.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-public.lock \
    && groupadd -g 10001 app && useradd -u 10001 -g app -m app
COPY backend/app /app/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["uvicorn", "app.public.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips=172.30.0.2"]

FROM api AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "-m", "app.public.worker"]
