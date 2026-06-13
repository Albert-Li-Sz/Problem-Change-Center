#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_BASE_IMAGE="${P2H_PYTHON_BASE_IMAGE:-python:3.12-slim-bookworm}"
OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
ARCH_NAME="$(uname -m 2>/dev/null || printf 'unknown')"

BUILD_RUNNER=1
BUILD_WINE=0
INSTALL_BACKEND=1
INSTALL_FRONTEND=1
BUILD_FRONTEND=1

log() {
  printf '\033[1;34m==>\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2
}

die() {
  printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

is_macos() {
  [[ "$OS_NAME" == "Darwin" ]]
}

is_linux() {
  [[ "$OS_NAME" == "Linux" ]]
}

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install backend dependencies, frontend dependencies, and Docker runner images.

Options:
  --wine                 Also build p2h-runner-wine and configure .env to use it.
  --skip-runner          Skip Docker runner image build.
  --skip-backend         Skip backend virtualenv and pip install.
  --skip-frontend        Skip frontend npm install.
  --no-frontend-build    Skip npm run build after installing frontend deps.
  --python PATH          Python interpreter used to create backend/.venv.
  --base-image IMAGE     Docker base image used for runner builds.
  -h, --help             Show this help.

Examples:
  ./install.sh
  ./install.sh --wine
  ./install.sh --skip-runner
  ./install.sh --base-image registry.example.com/library/python:3.12-slim-bookworm
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wine)
      BUILD_WINE=1
      ;;
    --skip-runner)
      BUILD_RUNNER=0
      ;;
    --skip-backend)
      INSTALL_BACKEND=0
      ;;
    --skip-frontend)
      INSTALL_FRONTEND=0
      BUILD_FRONTEND=0
      ;;
    --no-frontend-build)
      BUILD_FRONTEND=0
      ;;
    --python)
      [[ $# -ge 2 ]] || die "--python requires a path"
      PYTHON_BIN="$2"
      shift
      ;;
    --base-image)
      [[ $# -ge 2 ]] || die "--base-image requires an image name"
      PYTHON_BASE_IMAGE="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

require_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    return
  fi

  case "$1" in
    docker)
      if is_macos; then
        die "missing required command: docker. Install Docker Desktop for Mac and start it."
      elif is_linux; then
        die "missing required command: docker. Install Docker Engine and the Docker Compose plugin."
      fi
      ;;
    npm)
      if is_macos; then
        die "missing required command: npm. Install Node.js, for example with Homebrew: brew install node"
      elif is_linux; then
        die "missing required command: npm. Install Node.js/npm with your distribution package manager or NodeSource."
      fi
      ;;
    "$PYTHON_BIN")
      if is_macos; then
        die "missing required command: $1. Install Python 3.10 or newer, for example with Homebrew: brew install python"
      elif is_linux; then
        die "missing required command: $1. Install Python 3.10+ and venv support, for example python3 python3-venv."
      fi
      ;;
  esac

  die "missing required command: $1"
}

check_python() {
  require_cmd "$PYTHON_BIN"
  "$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY
}

compose_command() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return
  fi

  if is_macos; then
    die "Docker Compose is required. Install or update Docker Desktop for Mac."
  elif is_linux; then
    die "Docker Compose is required. Install the docker compose plugin or docker-compose."
  fi
  die "Docker Compose is required. Install Docker Desktop or the docker compose plugin."
}

print_docker_unreachable_help() {
  if is_macos; then
    cat >&2 <<'EOF'
error: Docker daemon is not reachable by the current user.

On macOS:
  1. Start Docker Desktop and wait until it finishes starting.
  2. Verify from Terminal:
       docker info

Do not run this project with sudo on macOS; Docker Desktop should be reachable
from the normal user shell.
EOF
    return
  fi

  if is_linux; then
    cat >&2 <<'EOF'
error: Docker daemon is not reachable by the current user.

On Linux, the backend must be able to run `docker run` without an interactive
sudo prompt. Use one of these setups:
  1. Add this user to the docker group, then log out and back in:
       sudo usermod -aG docker "$USER"
       newgrp docker
  2. Or run this installer and ./scripts/start.sh from a service/user that can
     access /var/run/docker.sock.

Verify before retrying:
  docker info

Do not run only the backend with sudo while the project files remain owned by a
different user; that commonly creates root-owned uploads, logs, and venv files.
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
  require_cmd docker
  if docker info >/dev/null 2>&1; then
    return
  fi

  print_docker_unreachable_help
  exit 1
}

install_backend() {
  log "Installing backend Python dependencies"
  check_python

  if [[ ! -d "$ROOT_DIR/backend/.venv" ]]; then
    if ! "$PYTHON_BIN" -m venv "$ROOT_DIR/backend/.venv"; then
      if is_linux; then
        die "failed to create backend/.venv. On Debian/Ubuntu, install python3-venv and retry."
      fi
      die "failed to create backend/.venv"
    fi
  fi

  "$ROOT_DIR/backend/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT_DIR/backend/.venv/bin/python" -m pip install -r "$ROOT_DIR/backend/requirements.txt"
}

install_frontend() {
  log "Installing frontend dependencies"
  require_cmd npm

  (
    cd "$ROOT_DIR/frontend"
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi

    if [[ "$BUILD_FRONTEND" -eq 1 ]]; then
      npm run build
    fi
  )
}

build_runner() {
  log "Building Docker runner image"
  check_docker_access
  compose_command
  export P2H_PYTHON_BASE_IMAGE="$PYTHON_BASE_IMAGE"

  if [[ "$BUILD_WINE" -eq 1 && "$ARCH_NAME" != "x86_64" && "$ARCH_NAME" != "amd64" ]]; then
    warn "Wine runner is linux/amd64; on $OS_NAME/$ARCH_NAME Docker must provide amd64 emulation, and builds/runs will be slower."
  fi

  if ! "${COMPOSE_CMD[@]}" --profile runner build runner; then
    cat >&2 <<'EOF'

Docker runner build failed. Common causes:
  - Docker Hub token/metadata request timed out.
  - GitHub download/clone for pinned converter commits failed.
  - Docker daemon lacks network access.

Retry:
  ./install.sh

If you only want to install Python/Node dependencies first:
  ./install.sh --skip-runner

If Docker Hub is timing out, use an accessible mirror for the Python base image:
  ./install.sh --base-image <registry>/library/python:3.12-slim-bookworm
EOF
    exit 1
  fi

  if [[ "$BUILD_WINE" -eq 1 ]]; then
    log "Building Wine Docker runner image"
    if ! "${COMPOSE_CMD[@]}" --profile wine build runner-wine; then
      cat >&2 <<'EOF'

Wine runner build failed. You can still use the normal runner for packages that
do not execute Windows .exe files.

Retry only the runner build later:
  docker compose --profile wine build runner-wine
EOF
      exit 1
    fi
  fi
}

write_env_file() {
  local env_file="$ROOT_DIR/.env"
  local runner_image="p2h-runner"
  if [[ "$BUILD_WINE" -eq 1 ]]; then
    runner_image="p2h-runner-wine"
  fi

  if [[ -f "$env_file" ]]; then
    log "Keeping existing .env"
    if [[ "$BUILD_WINE" -eq 1 ]] && ! grep -q '^P2H_RUNNER_IMAGE=p2h-runner-wine$' "$env_file"; then
      warn "Wine runner was built, but existing .env was not changed. Set P2H_RUNNER_IMAGE=p2h-runner-wine manually if needed."
    fi
    if [[ "$BUILD_WINE" -eq 1 ]] && ! grep -q '^P2H_DOCKER_WINE_PIDS_LIMIT=' "$env_file"; then
      warn "Wine runner works better with P2H_DOCKER_WINE_PIDS_LIMIT=4096, especially on macOS/Apple Silicon."
    fi
    if [[ "$PYTHON_BASE_IMAGE" != "python:3.12-slim-bookworm" ]] && ! grep -q '^P2H_PYTHON_BASE_IMAGE=' "$env_file"; then
      warn "Custom base image was used for this build, but existing .env was not changed. Add P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE if you want future manual builds to reuse it."
    fi
    return
  fi

  log "Writing local .env"
  cat >"$env_file" <<EOF
P2H_DATA_DIR=~/.p2h-web-ui/backend_data
P2H_RUNNER_IMAGE=$runner_image
P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE
P2H_MAX_UPLOAD_BYTES=536870912
P2H_JOB_TIMEOUT_SECONDS=600
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=1024
P2H_DOCKER_WINE_PIDS_LIMIT=4096
P2H_DOCKER_TMP_SIZE=512m
P2H_DOCKER_WORK_SIZE=1g

P2H_BACKEND_HOST=127.0.0.1
P2H_BACKEND_PORT=8000
P2H_FRONTEND_HOST=127.0.0.1
P2H_FRONTEND_PORT=5173
EOF
}

main() {
  log "Installing Polygon Converter Web UI"

  if [[ "$INSTALL_BACKEND" -eq 1 ]]; then
    install_backend
  fi

  if [[ "$INSTALL_FRONTEND" -eq 1 ]]; then
    install_frontend
  fi

  if [[ "$BUILD_RUNNER" -eq 1 ]]; then
    build_runner
  fi

  write_env_file

  cat <<EOF

Install complete.

Start the Web UI:
  ./scripts/start.sh

Open:
  http://127.0.0.1:5173
EOF
}

main
