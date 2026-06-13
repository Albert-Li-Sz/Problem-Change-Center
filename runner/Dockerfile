ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG P2H_REF=93aca21
ARG P2D_REF=8b0919a2a3e0946faaf677ec5cb2cad65fee7e30
ARG P2H_APT_MIRROR=
ARG P2H_APT_SECURITY_MIRROR=

RUN set -eux; \
  if [ -n "$P2H_APT_MIRROR" ]; then \
    apt_mirror="${P2H_APT_MIRROR%/}"; \
    apt_security_mirror="${P2H_APT_SECURITY_MIRROR:-${apt_mirror}-security}"; \
    apt_security_mirror="${apt_security_mirror%/}"; \
    apt_sources="/etc/apt/sources.list.d/debian.sources"; \
    if [ ! -f "$apt_sources" ]; then apt_sources="/etc/apt/sources.list"; fi; \
    if [ ! -f "$apt_sources" ]; then echo "apt sources file not found" >&2; exit 1; fi; \
    sed -i \
      -e "s|http://deb.debian.org/debian-security|${apt_security_mirror}|g" \
      -e "s|http://deb.debian.org/debian|${apt_mirror}|g" \
      "$apt_sources"; \
  fi; \
  apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    coreutils \
    g++ \
    gcc \
    git \
    make \
    openjdk-17-jdk-headless \
    unzip \
    zip \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
  "PyYAML>=6,<7" \
  "https://github.com/KisuraOP/polygon2hydro/archive/${P2H_REF}.tar.gz" \
  "git+https://github.com/cn-xcpc-tools/Polygon2DOMjudge.git@${P2D_REF}" \
  && apt-get purge -y --auto-remove git

RUN groupadd -g 10001 app \
  && useradd -u 10001 -g app -m app \
  && mkdir -p /work \
  && chown app:app /work

COPY entrypoint.sh /usr/local/bin/p2h-entrypoint
COPY format_bridge.py /opt/format_bridge.py
COPY p2h_safe.py /opt/p2h_safe.py
RUN chmod +x /usr/local/bin/p2h-entrypoint

USER app
WORKDIR /work
ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/work
ENV XDG_CACHE_HOME=/work/.cache

ENTRYPOINT ["p2h-entrypoint"]
