#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
RUN_BACKEND=1
RUN_FRONTEND=1

usage() {
  cat <<'EOF'
Usage: ./scripts/start.sh [options]

Start the local backend and frontend development servers.

Options:
  --backend-only     Start only FastAPI.
  --frontend-only    Start only Vite.
  -h, --help         Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only)
      RUN_FRONTEND=0
      ;;
    --frontend-only)
      RUN_BACKEND=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option: %s\n' "$1" >&2
      exit 1
      ;;
  esac
  shift
done

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

P2H_BACKEND_HOST="${P2H_BACKEND_HOST:-127.0.0.1}"
P2H_BACKEND_PORT="${P2H_BACKEND_PORT:-8000}"
P2H_FRONTEND_HOST="${P2H_FRONTEND_HOST:-127.0.0.1}"
P2H_FRONTEND_PORT="${P2H_FRONTEND_PORT:-5173}"

pids=()

is_macos() {
  [[ "$OS_NAME" == "Darwin" ]]
}

is_linux() {
  [[ "$OS_NAME" == "Linux" ]]
}

print_missing_docker_help() {
  if is_macos; then
    printf 'error: docker command not found. Install Docker Desktop for Mac and start it.\n' >&2
  elif is_linux; then
    printf 'error: docker command not found. Install Docker Engine and the Docker Compose plugin.\n' >&2
  else
    printf 'error: docker command not found. Install Docker before starting the backend.\n' >&2
  fi
}

print_docker_unreachable_help() {
  if is_macos; then
    cat >&2 <<'EOF'
error: Docker daemon is not reachable by the current user.

On macOS:
  1. Start Docker Desktop and wait until it finishes starting.
  2. Verify from Terminal:
       docker info

The backend needs Docker because each conversion starts a restricted runner.
EOF
    return
  fi

  if is_linux; then
    cat >&2 <<'EOF'
error: Docker daemon is not reachable by the current user.

On Linux, add this user to the docker group and start a new login session:
  sudo usermod -aG docker "$USER"
  newgrp docker

Then verify:
  docker info

The backend needs this because each conversion starts a restricted Docker runner.
EOF
    return
  fi

  cat >&2 <<'EOF'
error: Docker daemon is not reachable by the current user.

Verify Docker is installed, running, and reachable:
  docker info
EOF
}

check_docker_access() {
  if ! command -v docker >/dev/null 2>&1; then
    print_missing_docker_help
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    print_docker_unreachable_help
    exit 1
  fi
}

cleanup() {
  local pid
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}

trap cleanup EXIT INT TERM

start_backend() {
  [[ -x "$ROOT_DIR/backend/.venv/bin/uvicorn" ]] || {
    printf 'error: backend/.venv is missing. Run ./install.sh first.\n' >&2
    exit 1
  }

  (
    cd "$ROOT_DIR/backend"
    exec .venv/bin/uvicorn app.main:app --host "$P2H_BACKEND_HOST" --port "$P2H_BACKEND_PORT"
  ) &
  pids+=("$!")
}

start_frontend() {
  [[ -d "$ROOT_DIR/frontend/node_modules" ]] || {
    printf 'error: frontend/node_modules is missing. Run ./install.sh first.\n' >&2
    exit 1
  }

  (
    cd "$ROOT_DIR/frontend"
    exec npm run dev -- --host "$P2H_FRONTEND_HOST" --port "$P2H_FRONTEND_PORT"
  ) &
  pids+=("$!")
}

if [[ "$RUN_BACKEND" -eq 1 ]]; then
  check_docker_access
  start_backend
fi

if [[ "$RUN_FRONTEND" -eq 1 ]]; then
  start_frontend
fi

if [[ "$RUN_BACKEND" -eq 1 ]]; then
  printf 'Backend:  http://%s:%s\n' "$P2H_BACKEND_HOST" "$P2H_BACKEND_PORT"
fi
if [[ "$RUN_FRONTEND" -eq 1 ]]; then
  printf 'Frontend: http://%s:%s\n' "$P2H_FRONTEND_HOST" "$P2H_FRONTEND_PORT"
fi
printf '\nPress Ctrl+C to stop.\n'

while :; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      wait "$pid"
      exit $?
    fi
  done
  sleep 1
done
