#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="/etc/oj-package-converter/production.env"
BACKEND_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./scripts/production-check.sh [--backend-only] [ENV_FILE]

Validate deployment secrets, permissions, built assets, backend configuration,
Docker access, runner image, and the Node frontend server prerequisites.
The environment file is parsed as KEY=VALUE data and is never evaluated as shell.
EOF
}

fail() {
  printf 'production check failed: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only)
      BACKEND_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      fail "unknown option: $1"
      ;;
    *)
      ENV_FILE="$1"
      ;;
  esac
  shift
done

[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "environment file must be a regular non-symlink: $ENV_FILE"

if [[ "$(uname -s)" == "Linux" ]]; then
  env_mode="$(stat -c '%a' "$ENV_FILE")"
  case "$env_mode" in
    600|640)
      ;;
    *)
      fail "$ENV_FILE must have mode 600 or 640 (found $env_mode)"
      ;;
  esac
fi

while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" == *=* ]] || fail "invalid environment line: $line"
  key="${line%%=*}"
  value="${line#*=}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || fail "invalid environment key: $key"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "invalid control character in $key"
  if [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  export "$key=$value"
done <"$ENV_FILE"

required_values=(
  P2H_DEPLOYMENT_MODE
  P2H_DATA_DIR
  P2H_RUNNER_IMAGE
  P2H_ALLOWED_HOSTS
  P2H_ALLOWED_ORIGINS
  P2H_BACKEND_PORT
  P2H_FRONTEND_PORT
)
for key in "${required_values[@]}"; do
  [[ -n "${!key:-}" ]] || fail "$key is required"
done

case "$P2H_DEPLOYMENT_MODE" in
  internal|external) ;;
  *) fail "P2H_DEPLOYMENT_MODE must be internal or external" ;;
esac
[[ "$P2H_BACKEND_PORT" == 11451 ]] || fail "P2H_BACKEND_PORT must be 11451"
[[ "$P2H_FRONTEND_PORT" == 11452 ]] || fail "P2H_FRONTEND_PORT must be 11452"
if [[ "$P2H_DEPLOYMENT_MODE" == external ]]; then
  [[ "$P2H_ALLOWED_HOSTS" != *"*"* ]] || fail "P2H_ALLOWED_HOSTS must not contain * in external mode"
  [[ "$P2H_ALLOWED_ORIGINS" == http://*:11452 ]] || fail "P2H_ALLOWED_ORIGINS must use http://HOST:11452"
  [[ "${P2H_ACCESS_KEY_HASH:-}" =~ ^pbkdf2_sha256\$[0-9]{6,7}\$[0-9a-f]{32,64}\$[0-9a-f]{64}$ ]] || fail "P2H_ACCESS_KEY_HASH is invalid"
fi

[[ -x "$ROOT_DIR/backend/.venv/bin/python" ]] || fail "backend virtual environment is missing"
(
  cd "$ROOT_DIR/backend"
  .venv/bin/python -c 'from app.config import Settings; Settings.from_env()'
) || fail "backend production settings are invalid"

[[ -d "$P2H_DATA_DIR" && -w "$P2H_DATA_DIR" ]] || fail "P2H_DATA_DIR must exist and be writable"

docker_bin="${P2H_DOCKER_BIN:-docker}"
command -v "$docker_bin" >/dev/null 2>&1 || fail "Docker command is unavailable: $docker_bin"
"$docker_bin" info >/dev/null 2>&1 || fail "Docker daemon is unreachable"
"$docker_bin" image inspect "$P2H_RUNNER_IMAGE" >/dev/null 2>&1 || fail "runner image is missing: $P2H_RUNNER_IMAGE"
P2H_ISOLATION_AUDIT_ROOT="$P2H_DATA_DIR" \
  "$ROOT_DIR/scripts/test-runner-isolation.sh" "$P2H_RUNNER_IMAGE" >/dev/null || fail "runner isolation probe failed"

if [[ "$BACKEND_ONLY" -eq 0 ]]; then
  frontend_root="${P2H_FRONTEND_ROOT:-$ROOT_DIR/frontend/dist}"
  [[ -d "$frontend_root" && -f "$frontend_root/index.html" ]] || fail "frontend production build is missing"
  command -v node >/dev/null 2>&1 || fail "Node.js is unavailable"
fi

printf 'production check passed\n'
