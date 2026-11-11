#!/usr/bin/env bash
# One command for the public site: ./public.sh [install|upgrade|backup|status|...]
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "需要 Python 3：sudo apt-get install -y python3" >&2; exit 1; }
exec python3 "$ROOT_DIR/scripts/public.py" "$@"
