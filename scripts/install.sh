#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PYTHON_BASE_IMAGE="${P2H_PYTHON_BASE_IMAGE:-python:3.12-slim-bookworm}"

BUILD_RUNNER=1
BUILD_WINE=0
INSTALL_BACKEND=1
INSTALL_FRONTEND=1
BUILD_FRONTEND=1
START_DATABASE=1
MAX_CONCURRENT_JOBS=2
MAX_CONCURRENT_JOBS_SET=0

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
  --skip-db              Do not start the PostgreSQL container during install.
  --max-containers N     Maximum number of runner containers allowed at once.
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
    --skip-db)
      START_DATABASE=0
      ;;
    --max-containers)
      [[ $# -ge 2 ]] || die "--max-containers requires a number"
      [[ "$2" =~ ^[1-9][0-9]*$ ]] || die "--max-containers must be a positive integer"
      MAX_CONCURRENT_JOBS="$2"
      MAX_CONCURRENT_JOBS_SET=1
      shift
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
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
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
  die "Docker Compose is required. Install Docker Desktop or the docker compose plugin."
}

install_backend() {
  log "Installing backend Python dependencies"
  check_python

  if [[ ! -d "$ROOT_DIR/backend/.venv" ]]; then
    "$PYTHON_BIN" -m venv "$ROOT_DIR/backend/.venv"
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
  require_cmd docker
  docker info >/dev/null 2>&1 || die "Docker daemon is not running or not reachable"
  compose_command
  export P2H_PYTHON_BASE_IMAGE="$PYTHON_BASE_IMAGE"

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

generate_secret() {
  "$PYTHON_BIN" - <<'PY'
import secrets

print(secrets.token_urlsafe(32))
PY
}

configure_max_concurrent_jobs() {
  if [[ "$MAX_CONCURRENT_JOBS_SET" -eq 1 ]]; then
    return
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    return
  fi

  local answer
  printf 'Maximum concurrent runner containers [%s]: ' "$MAX_CONCURRENT_JOBS"
  IFS= read -r answer || answer=""
  answer="$(printf '%s' "$answer" | tr -d '[:space:]')"
  if [[ -z "$answer" ]]; then
    return
  fi
  [[ "$answer" =~ ^[1-9][0-9]*$ ]] || die "maximum concurrent containers must be a positive integer"
  MAX_CONCURRENT_JOBS="$answer"
}

start_database() {
  log "Starting PostgreSQL container"
  require_cmd docker
  docker info >/dev/null 2>&1 || die "Docker daemon is not running or not reachable"
  compose_command
  "${COMPOSE_CMD[@]}" up -d postgres
}

write_env_file() {
  local env_file="$ROOT_DIR/.env"
  local runner_image="p2h-runner"
  local auth_secret
  local bootstrap_admin_email="admin@p2h.local"
  local bootstrap_admin_password
  auth_secret="$(generate_secret)"
  bootstrap_admin_password="$(generate_secret)"
  if [[ "$BUILD_WINE" -eq 1 ]]; then
    runner_image="p2h-runner-wine"
  fi

  if [[ -f "$env_file" ]]; then
    log "Keeping existing .env"
    if [[ "$BUILD_WINE" -eq 1 ]] && ! grep -q '^P2H_RUNNER_IMAGE=p2h-runner-wine$' "$env_file"; then
      if grep -q '^P2H_RUNNER_IMAGE=p2h-runner$' "$env_file"; then
        log "Switching existing .env to p2h-runner-wine"
        sed -i.bak 's/^P2H_RUNNER_IMAGE=p2h-runner$/P2H_RUNNER_IMAGE=p2h-runner-wine/' "$env_file"
        rm -f "$env_file.bak"
      elif ! grep -q '^P2H_RUNNER_IMAGE=' "$env_file"; then
        log "Adding P2H_RUNNER_IMAGE=p2h-runner-wine to existing .env"
        printf '\nP2H_RUNNER_IMAGE=p2h-runner-wine\n' >>"$env_file"
      else
        warn "Wine runner was built, but existing .env uses a custom P2H_RUNNER_IMAGE. Set P2H_RUNNER_IMAGE=p2h-runner-wine manually if needed."
      fi
    fi
    if [[ "$PYTHON_BASE_IMAGE" != "python:3.12-slim-bookworm" ]] && ! grep -q '^P2H_PYTHON_BASE_IMAGE=' "$env_file"; then
      warn "Custom base image was used for this build, but existing .env was not changed. Add P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE if you want future manual builds to reuse it."
    fi
    for required_var in P2H_DATABASE_URL P2H_AUTH_SECRET_KEY P2H_AUTH_TOKEN_TTL_SECONDS P2H_MAX_CONCURRENT_JOBS P2H_DEFAULT_DAILY_QUOTA P2H_JOB_TTL_SECONDS P2H_BOOTSTRAP_ADMIN_EMAIL P2H_BOOTSTRAP_ADMIN_PASSWORD; do
      if ! grep -q "^${required_var}=" "$env_file"; then
        warn "Existing .env is missing $required_var. Add it manually or regenerate .env to enable the team-platform features."
      fi
    done
    if grep -q '^P2H_BOOTSTRAP_ADMIN_EMAIL=$' "$env_file" || grep -q '^P2H_BOOTSTRAP_ADMIN_PASSWORD=$' "$env_file"; then
      warn "Existing .env has an empty bootstrap administrator email or password. Set P2H_BOOTSTRAP_ADMIN_EMAIL and P2H_BOOTSTRAP_ADMIN_PASSWORD to guarantee an administrator account."
    fi
    return
  fi

  log "Writing local .env"
  cat >"$env_file" <<EOF
P2H_DATA_DIR=~/.p2h-web-ui/backend_data
P2H_RUNNER_IMAGE=$runner_image
P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE
P2H_MAX_UPLOAD_BYTES=134217728
P2H_JOB_TIMEOUT_SECONDS=600
P2H_JOB_TTL_SECONDS=86400
P2H_MAX_CONCURRENT_JOBS=$MAX_CONCURRENT_JOBS
P2H_DEFAULT_DAILY_QUOTA=10
P2H_AUTH_SECRET_KEY=$auth_secret
P2H_AUTH_TOKEN_TTL_SECONDS=604800
P2H_BOOTSTRAP_ADMIN_EMAIL=$bootstrap_admin_email
P2H_BOOTSTRAP_ADMIN_PASSWORD=$bootstrap_admin_password
P2H_DATABASE_URL=postgresql://p2h:p2h_dev_password@127.0.0.1:5432/p2h
P2H_POSTGRES_DB=p2h
P2H_POSTGRES_USER=p2h
P2H_POSTGRES_PASSWORD=p2h_dev_password
P2H_POSTGRES_HOST=127.0.0.1
P2H_POSTGRES_PORT=5432
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=256
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
  configure_max_concurrent_jobs

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

  if [[ "$START_DATABASE" -eq 1 ]]; then
    start_database
  fi

  cat <<EOF

Install complete.

Default administrator credentials are stored in .env as:
  P2H_BOOTSTRAP_ADMIN_EMAIL
  P2H_BOOTSTRAP_ADMIN_PASSWORD

Start the Web UI:
  ./scripts/start.sh

Open:
  http://127.0.0.1:5173
EOF
}

main
