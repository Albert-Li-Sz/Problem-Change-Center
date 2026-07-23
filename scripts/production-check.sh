#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="/etc/oj-package-converter/production.env"
BACKEND_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./scripts/production-check.sh [--backend-only] [ENV_FILE]

Validate production secrets, permissions, built assets, backend configuration,
Docker access, runner image, and (unless skipped) the Caddy configuration.
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
  export "$key=$value"
done <"$ENV_FILE"

required_values=(
  P2H_DEPLOYMENT_MODE
  P2H_DATA_DIR
  P2H_RUNNER_IMAGE
  P2H_ALLOWED_HOSTS
  P2H_ALLOWED_ORIGINS
  P2H_TRUSTED_PROXY_SECRET
)
for key in "${required_values[@]}"; do
  [[ -n "${!key:-}" ]] || fail "$key is required"
done

[[ "$P2H_DEPLOYMENT_MODE" == "production" ]] || fail "P2H_DEPLOYMENT_MODE must be production"
[[ "$P2H_ALLOWED_HOSTS" != *"*"* ]] || fail "P2H_ALLOWED_HOSTS must not contain *"
[[ "$P2H_ALLOWED_ORIGINS" == https://* ]] || fail "P2H_ALLOWED_ORIGINS must use HTTPS"
[[ "${#P2H_TRUSTED_PROXY_SECRET}" -ge 32 ]] || fail "P2H_TRUSTED_PROXY_SECRET is too short"
proxy_secret_upper="$(printf '%s' "$P2H_TRUSTED_PROXY_SECRET" | tr '[:lower:]' '[:upper:]')"
case "$proxy_secret_upper" in
  *CHANGE_ME*|*GENERATE*|*REPLACE*)
    fail "P2H_TRUSTED_PROXY_SECRET is still a placeholder"
    ;;
esac

[[ -x "$ROOT_DIR/backend/.venv/bin/python" ]] || fail "backend virtual environment is missing"
(
  cd "$ROOT_DIR/backend"
  .venv/bin/python -c 'from app.config import Settings; Settings.from_env()'
) || fail "backend production settings are invalid"

[[ -d "$P2H_DATA_DIR" && -w "$P2H_DATA_DIR" ]] || fail "P2H_DATA_DIR must exist and be writable"

docker_bin="${P2H_DOCKER_BIN:-docker}"
command -v "$docker_bin" >/dev/null 2>&1 || fail "Docker command is unavailable: $docker_bin"
"$docker_bin" info >/dev/null 2>&1 || fail "Docker daemon is unreachable"
if [[ "${P2H_REQUIRE_ROOTLESS_DOCKER:-1}" == "1" ]]; then
  docker_security="$("$docker_bin" info --format '{{json .SecurityOptions}}' 2>/dev/null)"
  [[ "$docker_security" == *rootless* ]] || fail "Docker must run in rootless mode"
  cgroup_driver="$("$docker_bin" info --format '{{.CgroupDriver}}' 2>/dev/null)"
  [[ "$cgroup_driver" == "systemd" ]] || fail "rootless Docker must use the systemd cgroup driver"
fi
"$docker_bin" image inspect "$P2H_RUNNER_IMAGE" >/dev/null 2>&1 || fail "runner image is missing: $P2H_RUNNER_IMAGE"
P2H_ISOLATION_AUDIT_ROOT="$P2H_DATA_DIR" \
  "$ROOT_DIR/scripts/test-runner-isolation.sh" "$P2H_RUNNER_IMAGE" >/dev/null || fail "runner isolation probe failed"

if [[ "$BACKEND_ONLY" -eq 0 ]]; then
  required_caddy_values=(
    P2H_SITE_ADDRESS
    P2H_FRONTEND_ROOT
    P2H_MAX_REQUEST_BODY_BYTES
    P2H_HSTS_MAX_AGE_SECONDS
    P2H_ADMIN_USER
    P2H_ADMIN_PASSWORD_HASH
    P2H_ACCESS_LOG
  )
  for key in "${required_caddy_values[@]}"; do
    [[ -n "${!key:-}" ]] || fail "$key is required"
  done
  password_hash_upper="$(printf '%s' "$P2H_ADMIN_PASSWORD_HASH" | tr '[:lower:]' '[:upper:]')"
  case "$password_hash_upper" in
    *CHANGE_ME*|*GENERATE*|*REPLACE*)
      fail "P2H_ADMIN_PASSWORD_HASH is still a placeholder"
      ;;
  esac
  [[ -d "$P2H_FRONTEND_ROOT" && -f "$P2H_FRONTEND_ROOT/index.html" ]] || fail "frontend production build is missing"
  command -v caddy >/dev/null 2>&1 || fail "Caddy is unavailable"
  caddy validate --config "$ROOT_DIR/deploy/Caddyfile" --adapter caddyfile >/dev/null
fi

printf 'production check passed\n'
