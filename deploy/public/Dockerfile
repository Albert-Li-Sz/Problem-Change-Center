FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS api
WORKDIR /app
# Overridable for networks where the default index is slow; hashes still apply.
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY backend/requirements-public.lock /app/requirements-public.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-public.lock \
    && groupadd -g 10001 app && useradd -u 10001 -g app -m app
COPY backend/app /app/app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENV PUBLIC_API_WORKERS=2
CMD ["sh", "-c", "exec uvicorn app.public.main:create_app --factory --host 0.0.0.0 --port 8000 --workers \"$PUBLIC_API_WORKERS\" --proxy-headers --forwarded-allow-ips=172.30.0.2"]

FROM api AS worker
USER root
ARG APT_MIRROR=
# Only the client is needed: the Worker talks to the host daemon's socket.
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org/debian|${APT_MIRROR%/}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "-m", "app.public.worker"]
