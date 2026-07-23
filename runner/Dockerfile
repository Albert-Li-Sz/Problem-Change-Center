ARG PYTHON_BASE_IMAGE=python:3.14-slim-trixie
FROM ${PYTHON_BASE_IMAGE}

ARG P2H_REF=93aca21
ARG P2D_REF=8b0919a2a3e0946faaf677ec5cb2cad65fee7e30
ARG P2H_APT_MIRROR=
ARG P2H_APT_SECURITY_MIRROR=

# Python 3.14.6 predates the upstream 3.14 backport for CVE-2026-15308.
# Pin both the commit and content digest so a remote change cannot alter the image.
ADD --checksum=sha256:5c5ed245889135564e75dfed9a47aeb6b4d3e5a2e9614d918a986767e3747539 \
  https://raw.githubusercontent.com/python/cpython/07efb08123ba9367a7107325adb9d5626dca1ca9/Lib/html/parser.py \
  /tmp/html-parser.py
RUN set -eux; \
  parser_path="$(python -c 'import html.parser; print(html.parser.__file__)')"; \
  install -m 0644 /tmp/html-parser.py "$parser_path"; \
  rm /tmp/html-parser.py; \
  python -c 'from html.parser import HTMLParser; assert hasattr(HTMLParser(), "_pending")'

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
    openjdk-21-jdk-headless \
    unzip \
    zip \
  && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "pip>=26.1.2" \
  && python -m pip install --no-cache-dir \
  "defusedxml>=0.7.1,<1" \
  "PyYAML>=6,<7" \
  "https://github.com/KisuraOP/polygon2hydro/archive/${P2H_REF}.tar.gz" \
  "git+https://github.com/cn-xcpc-tools/Polygon2DOMjudge.git@${P2D_REF}" \
  && python -m pip uninstall -y pip \
  && apt-get purge -y --auto-remove git

RUN groupadd -g 10001 app \
  && useradd -u 10001 -g app -m app \
  && mkdir -p /work \
  && chown app:app /work

COPY entrypoint.sh /usr/local/bin/p2h-entrypoint
COPY format_bridge.py /opt/format_bridge.py
COPY hoj_bridge.py /opt/hoj_bridge.py
COPY package_ir.py /opt/package_ir.py
COPY package_security.py /opt/package_security.py
COPY package_adapters.py /opt/package_adapters.py
COPY package_converter.py /opt/package_converter.py
COPY package_repair.py /opt/package_repair.py
COPY progress.py /opt/progress.py
COPY p2h_safe.py /opt/p2h_safe.py
RUN chmod +x /usr/local/bin/p2h-entrypoint

WORKDIR /work
ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/work
ENV XDG_CACHE_HOME=/work/.cache

ENTRYPOINT ["p2h-entrypoint"]
