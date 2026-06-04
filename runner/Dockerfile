FROM python:3.12-slim-bookworm

ARG P2H_REF=93aca21
ARG P2D_REF=8b0919a2a3e0946faaf677ec5cb2cad65fee7e30

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    coreutils \
    g++ \
    gcc \
    make \
    openjdk-17-jdk-headless \
    unzip \
    zip \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
  "https://github.com/KisuraOP/polygon2hydro/archive/${P2H_REF}.tar.gz" \
  "https://github.com/cn-xcpc-tools/Polygon2DOMjudge/archive/${P2D_REF}.tar.gz"

RUN groupadd -g 10001 app \
  && useradd -u 10001 -g app -m app \
  && mkdir -p /work \
  && chown app:app /work

COPY entrypoint.sh /usr/local/bin/p2h-entrypoint
COPY p2h_safe.py /opt/p2h_safe.py
RUN chmod +x /usr/local/bin/p2h-entrypoint

USER app
WORKDIR /work
ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/work
ENV XDG_CACHE_HOME=/work/.cache

ENTRYPOINT ["p2h-entrypoint"]
