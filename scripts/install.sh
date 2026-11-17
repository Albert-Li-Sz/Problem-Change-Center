#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DEFAULT_PYTHON_BASE_IMAGE="python:3.14-slim-trixie"
DEFAULT_PREBUILT_RUNNER_IMAGE="ghcr.io/albert-li-sz/p2h-runner:main"
DEFAULT_PREBUILT_WINE_IMAGE="ghcr.io/albert-li-sz/p2h-runner-wine:main"
PYTHON_BASE_IMAGE="${P2H_PYTHON_BASE_IMAGE:-$DEFAULT_PYTHON_BASE_IMAGE}"
PYTHON_BASE_IMAGE_EXPLICIT=0
if [[ -n "${P2H_PYTHON_BASE_IMAGE:-}" ]]; then
  PYTHON_BASE_IMAGE_EXPLICIT=1
fi
PYTHON_BASE_IMAGE_FALLBACKS="${P2H_PYTHON_BASE_IMAGE_FALLBACKS:-docker.m.daocloud.io/library/python:3.14-slim-trixie hub.rat.dev/library/python:3.14-slim-trixie}"
APT_MIRROR="${P2H_APT_MIRROR:-}"
APT_SECURITY_MIRROR="${P2H_APT_SECURITY_MIRROR:-}"
OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
ARCH_NAME="$(uname -m 2>/dev/null || printf 'unknown')"

RUNNER_SOURCE=build
BUILD_WINE=0
WINE_EXPLICIT=0
RUNNER_IMAGE_OVERRIDE="${P2H_PREBUILT_RUNNER_IMAGE:-}"
RUNNER_SOURCE_EXPLICIT=0
if [[ -n "$RUNNER_IMAGE_OVERRIDE" ]]; then
  RUNNER_SOURCE=prebuilt
  RUNNER_SOURCE_EXPLICIT=1
fi
BUILD_PROXY_MODE=auto
EFFECTIVE_USE_BUILD_PROXY=1
INSTALL_BACKEND=1
INSTALL_FRONTEND=1
BUILD_FRONTEND=1
INTERACTIVE_MODE=auto
INTERACTIVE_ENABLED=0
BIND_ADDRESS="${P2H_INSTALL_BIND:-}"
BIND_EXPLICIT=0
if [[ -n "$BIND_ADDRESS" ]]; then
  BIND_EXPLICIT=1
fi
CONFIG_FILE="${P2H_CONFIG_FILE:-$ROOT_DIR/.env}"
FORCE_CONFIG=0

COMMAND=install
COMMAND_ARGS=()
SYSTEMD_MODE=auto
SYSTEMD_ACTIVE=0
SYSTEMD_UNIT_DIR="${P2H_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
BACKEND_UNIT=oj-package-converter.service
FRONTEND_UNIT=oj-package-converter-frontend.service
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
GIT_PULL=1
ASSUME_YES=0
PURGE=0
REMOVE_IMAGES=0

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
Usage:
  ./install.sh [install] [options]   Install or reinstall the Web UI.
  ./install.sh update [options]      Pull the latest code and refresh everything.
  ./install.sh uninstall [options]   Remove services and generated files.

Install runs interactively when stdin is a terminal. Both services listen on
127.0.0.1 (this machine only) unless 0.0.0.0 is chosen to share the Web UI with
a trusted LAN. Without a terminal, prompts are skipped and defaults are used.

Install options:
  --interactive          Force the interactive installation wizard.
  --non-interactive      Disable all prompts.
  --bind ADDRESS         Listen address: 127.0.0.1 (default) or 0.0.0.0.
  --local                Alias for --bind 127.0.0.1 (this machine only).
  --lan                  Alias for --bind 0.0.0.0 (trusted LAN access).
  --systemd              Install and start systemd services (Linux only).
  --no-systemd           Do not install or refresh systemd services.
  --wine                 Select the Wine-capable runner image.
  --no-wine              Select the normal runner without prompting.
  --runner-source MODE   Use prebuilt, build, or skip for the runner image.
  --runner-image IMAGE   Pull a prebuilt runner tag/digest instead of building.
  --skip-runner          Skip Docker runner image setup.
  --skip-backend         Skip backend virtualenv and pip install.
  --skip-frontend        Skip frontend npm install.
  --no-frontend-build    Skip npm run build after installing frontend deps.
  --python PATH          Python interpreter used to create backend/.venv.
  --base-image IMAGE     Docker base image used for runner builds.
  --apt-mirror URL       Debian mirror used inside runner Docker builds.
  --apt-security URL     Debian security mirror used inside runner Docker builds.
  --build-proxy          Force passing host proxy env vars into Docker builds.
  --no-build-proxy       Do not pass host proxy env vars into Docker builds.
  --config PATH          Path of the generated configuration (default: .env).
  --force-config         Replace an existing configuration file.

Update options (the runner, build and --skip-* options above also apply):
  --no-pull              Do not run git pull; only refresh dependencies.
  The configuration is kept. The runner image named in it is pulled again
  (registry image) or rebuilt (p2h-runner / p2h-runner-wine). Installed
  systemd services are refreshed and restarted.

Uninstall options:
  --purge                Also delete the configuration and the data directory
                         (all jobs, uploads, logs and converted packages).
  --remove-images        Also remove the configured and local runner images.
  -y, --yes              Do not ask for confirmation.
  The source checkout itself is never deleted.

  -h, --help             Show this help.

Examples:
  ./install.sh
  ./install.sh --lan --systemd
  ./install.sh --wine
  ./install.sh --runner-image ghcr.io/albert-li-sz/p2h-runner:main
  ./install.sh --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian
  ./install.sh update
  ./install.sh update --no-pull --skip-runner
  ./install.sh uninstall
  ./install.sh uninstall --purge --remove-images --yes

If the default Docker Hub base image is unreachable, the installer will retry
with known mirror base images unless --base-image or P2H_PYTHON_BASE_IMAGE was
set explicitly. Override the retry list with P2H_PYTHON_BASE_IMAGE_FALLBACKS.

systemd services run as the user who runs the installer, directly from this
checkout, with the generated configuration as their EnvironmentFile. Writing
unit files and calling systemctl uses sudo; do not run the installer with sudo.

The Web UI has no login. Only use --lan on a trusted network.
P2H_INSTALL_BIND=127.0.0.1|0.0.0.0 selects the listen address like --bind.
EOF
}

case "${1:-}" in
  install|update|uninstall)
    COMMAND="$1"
    shift
    ;;
  help)
    usage
    exit 0
    ;;
esac
COMMAND_ARGS=("$@")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interactive)
      INTERACTIVE_MODE=on
      ;;
    --non-interactive)
      INTERACTIVE_MODE=off
      ;;
    --bind)
      [[ $# -ge 2 ]] || die "--bind requires 127.0.0.1 or 0.0.0.0"
      BIND_ADDRESS="$2"
      BIND_EXPLICIT=1
      shift
      ;;
    --local)
      BIND_ADDRESS=127.0.0.1
      BIND_EXPLICIT=1
      ;;
    --lan)
      BIND_ADDRESS=0.0.0.0
      BIND_EXPLICIT=1
      ;;
    --wine)
      BUILD_WINE=1
      WINE_EXPLICIT=1
      ;;
    --no-wine)
      BUILD_WINE=0
      WINE_EXPLICIT=1
      ;;
    --runner-source)
      [[ $# -ge 2 ]] || die "--runner-source requires prebuilt, build, or skip"
      case "$2" in
        prebuilt)
          RUNNER_SOURCE=prebuilt
          ;;
        build)
          RUNNER_SOURCE=build
          RUNNER_IMAGE_OVERRIDE=""
          ;;
        skip)
          RUNNER_SOURCE=skip
          RUNNER_IMAGE_OVERRIDE=""
          ;;
        *)
          die "--runner-source must be prebuilt, build, or skip"
          ;;
      esac
      RUNNER_SOURCE_EXPLICIT=1
      shift
      ;;
    --runner-image)
      [[ $# -ge 2 ]] || die "--runner-image requires an image name"
      RUNNER_IMAGE_OVERRIDE="$2"
      RUNNER_SOURCE=prebuilt
      RUNNER_SOURCE_EXPLICIT=1
      shift
      ;;
    --skip-runner)
      RUNNER_SOURCE=skip
      RUNNER_IMAGE_OVERRIDE=""
      RUNNER_SOURCE_EXPLICIT=1
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
      PYTHON_BASE_IMAGE_EXPLICIT=1
      shift
      ;;
    --apt-mirror)
      [[ $# -ge 2 ]] || die "--apt-mirror requires a URL"
      APT_MIRROR="$2"
      shift
      ;;
    --apt-security)
      [[ $# -ge 2 ]] || die "--apt-security requires a URL"
      APT_SECURITY_MIRROR="$2"
      shift
      ;;
    --build-proxy)
      BUILD_PROXY_MODE=on
      ;;
    --no-build-proxy)
      BUILD_PROXY_MODE=off
      ;;
    --config|--production-env)
      [[ $# -ge 2 ]] || die "$1 requires a path"
      CONFIG_FILE="$2"
      shift
      ;;
    --force-config)
      FORCE_CONFIG=1
      ;;
    --systemd)
      SYSTEMD_MODE=on
      ;;
    --no-systemd)
      SYSTEMD_MODE=off
      ;;
    --no-pull)
      GIT_PULL=0
      ;;
    --purge)
      PURGE=1
      ;;
    --remove-images)
      REMOVE_IMAGES=1
      ;;
    -y|--yes)
      ASSUME_YES=1
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

case "$BIND_ADDRESS" in
  ""|127.0.0.1|0.0.0.0)
    ;;
  *)
    die "bind address must be 127.0.0.1 or 0.0.0.0"
    ;;
esac

if [[ "$COMMAND" != uninstall && ( "$PURGE" -eq 1 || "$REMOVE_IMAGES" -eq 1 || "$ASSUME_YES" -eq 1 ) ]]; then
  die "--purge, --remove-images and --yes are only valid with: ./install.sh uninstall"
fi
if [[ "$COMMAND" != update && "$GIT_PULL" -eq 0 ]]; then
  die "--no-pull is only valid with: ./install.sh update"
fi
if [[ "$COMMAND" == uninstall && "$SYSTEMD_MODE" == on ]]; then
  die "--systemd is not valid with: ./install.sh uninstall"
fi

prompt_choice() {
  local target="$1"
  local prompt="$2"
  local default="$3"
  local answer=""

  while :; do
    printf '%s [%s]: ' "$prompt" "$default" >&2
    if ! IFS= read -r answer; then
      die "interactive input ended before installation choices were complete"
    fi
    answer="${answer:-$default}"
    case "$answer" in
      1|2|3)
        printf -v "$target" '%s' "$answer"
        return
        ;;
      *)
        warn "please enter 1, 2, or 3"
        ;;
    esac
  done
}

prompt_yes_no() {
  local target="$1"
  local prompt="$2"
  local default="$3"
  local answer=""

  while :; do
    printf '%s [%s]: ' "$prompt" "$default" >&2
    if ! IFS= read -r answer; then
      die "interactive input ended before installation choices were complete"
    fi
    answer="${answer:-$default}"
    case "$answer" in
      y|Y|yes|YES|Yes)
        printf -v "$target" '%s' 1
        return
        ;;
      n|N|no|NO|No)
        printf -v "$target" '%s' 0
        return
        ;;
      *)
        warn "please enter y or n"
        ;;
    esac
  done
}

resolve_interactive_mode() {
  case "$INTERACTIVE_MODE" in
    on)
      INTERACTIVE_ENABLED=1
      ;;
    off)
      INTERACTIVE_ENABLED=0
      ;;
    auto)
      if [[ -t 0 && -t 1 ]]; then
        INTERACTIVE_ENABLED=1
      fi
      ;;
    *)
      die "invalid interactive mode: $INTERACTIVE_MODE"
      ;;
  esac
}

configure_install() {
  local choice=""
  local confirmed=0
  local install_service=0

  resolve_interactive_mode

  if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -eq 0 ]]; then
    if [[ "$INTERACTIVE_ENABLED" -eq 1 ]]; then
      prompt_yes_no confirmed "Configuration $CONFIG_FILE exists; replace it" n
      if [[ "$confirmed" -eq 1 ]]; then
        FORCE_CONFIG=1
      else
        log "Keeping existing configuration: $CONFIG_FILE"
      fi
    elif [[ "$BIND_EXPLICIT" -eq 1 ]]; then
      die "$CONFIG_FILE already exists; pass --force-config to replace it"
    fi
  fi

  if [[ "$INTERACTIVE_ENABLED" -eq 1 ]]; then
    if [[ "$BIND_EXPLICIT" -eq 0 && ( ! -f "$CONFIG_FILE" || "$FORCE_CONFIG" -eq 1 ) ]]; then
      printf '\nListen address:\n  1) 127.0.0.1 - this machine only\n  2) 0.0.0.0   - trusted LAN / team access (no login)\n' >&2
      prompt_choice choice "Choose listen address" 1
      [[ "$choice" == 2 ]] && BIND_ADDRESS=0.0.0.0 || BIND_ADDRESS=127.0.0.1
    fi
    if [[ "$WINE_EXPLICIT" -eq 0 ]]; then
      prompt_yes_no BUILD_WINE "Enable Wine runner support" n
    fi
    if [[ "$RUNNER_SOURCE_EXPLICIT" -eq 0 ]]; then
      printf '\nRunner image:\n  1) Pull prebuilt image\n  2) Build locally\n  3) Skip runner setup\n' >&2
      prompt_choice choice "Choose runner source" 1
      case "$choice" in
        1) RUNNER_SOURCE=prebuilt ;;
        2) RUNNER_SOURCE=build ;;
        3) RUNNER_SOURCE=skip ;;
      esac
    fi
    if [[ "$SYSTEMD_MODE" == auto ]] && systemd_available; then
      prompt_yes_no install_service "Install systemd services (run in background, start on boot)" n
      if [[ "$install_service" -eq 1 ]]; then
        SYSTEMD_MODE=on
      else
        SYSTEMD_MODE=off
      fi
    fi
  fi

  if [[ "$SYSTEMD_MODE" == on ]] && ! systemd_available; then
    die "--systemd requires Linux with a running systemd (systemctl and /run/systemd/system)"
  fi

  BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"

  if [[ "$RUNNER_SOURCE" == prebuilt && -z "$RUNNER_IMAGE_OVERRIDE" ]]; then
    if [[ "$BUILD_WINE" -eq 1 ]]; then
      RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_WINE_IMAGE"
    else
      RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_RUNNER_IMAGE"
    fi
  fi

}

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

check_node() {
  require_cmd node
  require_cmd npm
  if node -e '
const [major, minor] = process.versions.node.split(".").map(Number);
const supported =
  (major === 20 && minor >= 19) ||
  (major === 22 && minor >= 12) ||
  major >= 24;
process.exit(supported ? 0 : 1);
'; then
    return
  fi

  die "Node.js 20.19+, 22.12+, or 24+ is required by the frontend dependencies (found $(node -p 'process.versions.node' 2>/dev/null || printf unknown))"
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

current_proxy_env() {
  local proxy=""
  proxy="${HTTP_PROXY:-${http_proxy:-}}"
  if [[ -z "$proxy" ]]; then
    proxy="${HTTPS_PROXY:-${https_proxy:-}}"
  fi
  if [[ -z "$proxy" ]]; then
    proxy="${ALL_PROXY:-${all_proxy:-}}"
  fi
  printf '%s' "$proxy"
}

proxy_url_host() {
  local proxy="$1"
  local hostport host

  hostport="${proxy#*://}"
  hostport="${hostport%%/*}"
  hostport="${hostport##*@}"

  if [[ "$hostport" == \[*\]* ]]; then
    host="${hostport#\[}"
    host="${host%%\]*}"
  else
    host="${hostport%%:*}"
  fi

  printf '%s' "$host" | tr '[:upper:]' '[:lower:]'
}

proxy_is_loopback() {
  local host
  host="$(proxy_url_host "$1")"

  case "$host" in
    localhost|127.*|0.0.0.0|::1)
      return 0
      ;;
  esac

  return 1
}

loopback_proxy_env() {
  local name value

  for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    value="${!name:-}"
    if [[ -n "$value" ]] && proxy_is_loopback "$value"; then
      printf '%s=%s' "$name" "$value"
      return 0
    fi
  done

  return 1
}

compose_build() {
  local profile="$1"
  local service="$2"

  if [[ "$EFFECTIVE_USE_BUILD_PROXY" -eq 1 ]]; then
    "${COMPOSE_CMD[@]}" --profile "$profile" build "$service"
    return
  fi

  env \
    -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
    -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
    "${COMPOSE_CMD[@]}" --profile "$profile" build "$service"
}

compose_build_with_base_fallback() {
  local profile="$1"
  local service="$2"
  local prefer_base_mirror=0
  local candidate original_base_image seen_candidates=""
  original_base_image="$PYTHON_BASE_IMAGE"

  if [[ "$PYTHON_BASE_IMAGE_EXPLICIT" -eq 0 && -n "$APT_MIRROR" && "$original_base_image" == "$DEFAULT_PYTHON_BASE_IMAGE" ]]; then
    prefer_base_mirror=1
  fi

  if [[ "$prefer_base_mirror" -eq 1 ]]; then
    for candidate in $PYTHON_BASE_IMAGE_FALLBACKS "$original_base_image"; do
      [[ -n "$candidate" ]] || continue
      case " $seen_candidates " in
        *" $candidate "*)
          continue
          ;;
      esac
      seen_candidates="$seen_candidates $candidate"

      if [[ "$candidate" == "$original_base_image" ]]; then
        warn "Base image mirrors failed; trying Docker Hub base image for $service: $candidate"
      else
        warn "A Debian apt mirror is configured; trying $service with base image mirror first: $candidate"
      fi
      PYTHON_BASE_IMAGE="$candidate"
      export P2H_PYTHON_BASE_IMAGE="$PYTHON_BASE_IMAGE"
      if compose_build "$profile" "$service"; then
        return 0
      fi
    done
    return 1
  fi

  for candidate in "$original_base_image" $PYTHON_BASE_IMAGE_FALLBACKS; do
    [[ -n "$candidate" ]] || continue
    case " $seen_candidates " in
      *" $candidate "*)
        continue
        ;;
    esac
    seen_candidates="$seen_candidates $candidate"

    if [[ "$candidate" != "$original_base_image" ]]; then
      warn "Docker Hub base image is unreachable; retrying $service with base image mirror: $candidate"
    fi
    PYTHON_BASE_IMAGE="$candidate"
    export P2H_PYTHON_BASE_IMAGE="$PYTHON_BASE_IMAGE"
    if compose_build "$profile" "$service"; then
      return 0
    fi
    if [[ "$PYTHON_BASE_IMAGE_EXPLICIT" -eq 1 ]]; then
      return 1
    fi
  done

  return 1
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
  "$ROOT_DIR/backend/.venv/bin/python" -m pip install \
    --require-hashes \
    -r "$ROOT_DIR/backend/requirements.lock"
}

install_frontend() {
  log "Installing frontend dependencies"
  check_node

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
  export P2H_APT_MIRROR="$APT_MIRROR"
  export P2H_APT_SECURITY_MIRROR="$APT_SECURITY_MIRROR"

  if [[ "$BUILD_WINE" -eq 1 && "$ARCH_NAME" != "x86_64" && "$ARCH_NAME" != "amd64" ]]; then
    warn "Wine runner is linux/amd64; on $OS_NAME/$ARCH_NAME Docker must provide amd64 emulation, and builds/runs will be slower."
  fi

  local proxy_env
  proxy_env="$(current_proxy_env)"
  local loopback_proxy
  case "$BUILD_PROXY_MODE" in
    auto)
      EFFECTIVE_USE_BUILD_PROXY=1
      if loopback_proxy="$(loopback_proxy_env)"; then
        EFFECTIVE_USE_BUILD_PROXY=0
        warn "Docker build proxy points at host loopback ($loopback_proxy), which build containers cannot reach."
        warn "Disabling proxy env vars for runner builds. Use --build-proxy only if your proxy is reachable from containers."
      fi
      ;;
    on)
      EFFECTIVE_USE_BUILD_PROXY=1
      ;;
    off)
      EFFECTIVE_USE_BUILD_PROXY=0
      ;;
    *)
      die "invalid build proxy mode: $BUILD_PROXY_MODE"
      ;;
  esac

  if [[ "$EFFECTIVE_USE_BUILD_PROXY" -eq 1 && -n "$proxy_env" ]]; then
    warn "Docker build may inherit proxy settings: $proxy_env"
    warn "If apt-get times out while connecting to that proxy, retry with --no-build-proxy or set a container-reachable proxy."
  elif [[ "$BUILD_PROXY_MODE" == "off" && -n "$proxy_env" ]]; then
    warn "Docker build proxy inheritance is disabled by --no-build-proxy."
  fi
  if [[ -n "$APT_MIRROR" ]]; then
    log "Using Debian apt mirror for runner builds: $APT_MIRROR"
  fi

  if ! compose_build_with_base_fallback runner runner; then
    cat >&2 <<'EOF'

Docker runner build failed. Common causes:
  - Docker build inherited a host proxy that containers cannot reach.
  - Debian apt sources are unreachable from the Docker build network.
  - Docker Hub or GitHub download requests timed out.

Retry:
  ./install.sh

If you only want to install Python/Node dependencies first:
  ./install.sh --skip-runner

If the log contains "Could not connect to <ip>:<port>", retry without host proxy:
  ./install.sh --no-build-proxy

If you need a proxy during Docker builds, use one that containers can reach and
retry with:
  ./install.sh --build-proxy

If Debian apt sources are slow or blocked, use an accessible apt mirror:
  ./install.sh --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian

If Docker Hub is timing out, use an accessible mirror for the Python base image:
  ./install.sh --base-image <registry>/library/python:3.14-slim-trixie
EOF
    exit 1
  fi

  if [[ "$BUILD_WINE" -eq 1 ]]; then
    log "Building Wine Docker runner image"
    if ! compose_build_with_base_fallback wine runner-wine; then
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

pull_runner() {
  local reference="$RUNNER_IMAGE_OVERRIDE"
  local basename="${reference##*/}"
  local image_name="${basename%%@*}"
  local suffix=""
  image_name="${image_name%%:*}"
  log "Pulling prebuilt Docker runner image: $RUNNER_IMAGE_OVERRIDE"
  check_docker_access
  if [[ "$image_name" == *-wine ]]; then
    docker pull --platform linux/amd64 "$RUNNER_IMAGE_OVERRIDE"
  else
    docker pull "$RUNNER_IMAGE_OVERRIDE"
  fi
  # Tagged Wine runners route non-Polygon requests to the corresponding plain
  # image. A digest identifies only one image and is never rewritten by the API.
  if [[ "$reference" == *@* ]]; then
    return
  fi
  if [[ "$basename" == *:* ]]; then
    suffix=":${basename##*:}"
    reference="${reference%:*}"
  fi
  if [[ "$reference" == *-wine ]]; then
    reference="${reference%-wine}$suffix"
    log "Pulling companion Docker runner image: $reference"
    docker pull "$reference"
  fi
}

# Configurations generated before 0.8.2 pinned the runner to the old fixed
# defaults. Switch exactly those values to automatic sizing; anything the
# administrator changed is left alone.
migrate_runner_resources() {
  local env_file="$1"
  local temp_file=""
  grep -Fxq 'P2H_DOCKER_MEMORY=3g' "$env_file" || return 0
  grep -Fxq 'P2H_DOCKER_CPUS=2' "$env_file" || return 0
  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  chmod 600 "$temp_file"
  awk '
    $0 == "P2H_DOCKER_MEMORY=3g" { print "P2H_DOCKER_MEMORY=auto"; next }
    $0 == "P2H_DOCKER_CPUS=2" { print "P2H_DOCKER_CPUS=auto"; next }
    { print }
  ' "$env_file" >"$temp_file"
  mv "$temp_file" "$env_file"
  log "Runner CPU and memory now follow this machine: P2H_DOCKER_CPUS=auto, P2H_DOCKER_MEMORY=auto"
}

write_env_file() {
  local env_file="$CONFIG_FILE"
  local runner_image="p2h-runner"
  local temp_file=""
  if [[ -n "$RUNNER_IMAGE_OVERRIDE" ]]; then
    runner_image="$RUNNER_IMAGE_OVERRIDE"
  elif [[ "$BUILD_WINE" -eq 1 ]]; then
    runner_image="p2h-runner-wine"
  fi

  if [[ -f "$env_file" && "$FORCE_CONFIG" -eq 0 ]]; then
    log "Keeping existing configuration: $env_file"
    if [[ -n "$RUNNER_IMAGE_OVERRIDE" ]] && ! grep -Fxq "P2H_RUNNER_IMAGE=$RUNNER_IMAGE_OVERRIDE" "$env_file"; then
      warn "Prebuilt runner was pulled, but existing .env was not changed. Set P2H_RUNNER_IMAGE=$RUNNER_IMAGE_OVERRIDE manually."
    fi
    if [[ "$BUILD_WINE" -eq 1 ]] && ! grep -q '^P2H_RUNNER_IMAGE=p2h-runner-wine$' "$env_file"; then
      warn "Wine runner was built, but existing .env was not changed. Set P2H_RUNNER_IMAGE=p2h-runner-wine manually if needed."
    fi
    if [[ "$BUILD_WINE" -eq 1 ]] && ! grep -q '^P2H_DOCKER_WINE_PIDS_LIMIT=' "$env_file"; then
      warn "Wine runner works better with P2H_DOCKER_WINE_PIDS_LIMIT=4096, especially on macOS/Apple Silicon."
    fi
    if [[ "$PYTHON_BASE_IMAGE" != "$DEFAULT_PYTHON_BASE_IMAGE" ]] && ! grep -q '^P2H_PYTHON_BASE_IMAGE=' "$env_file"; then
      warn "Custom base image was used for this build, but existing .env was not changed. Add P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE if you want future manual builds to reuse it."
    fi
    if [[ -n "$APT_MIRROR" ]] && ! grep -q '^P2H_APT_MIRROR=' "$env_file"; then
      warn "Custom apt mirror was used for this build, but existing .env was not changed. Add P2H_APT_MIRROR=$APT_MIRROR if you want future manual builds to reuse it."
    fi
    migrate_runner_resources "$env_file"
    return
  fi

  log "Writing deployment configuration: $env_file"
  mkdir -p "$(dirname "$env_file")"
  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  chmod 600 "$temp_file"
  cat >"$temp_file" <<EOF
P2H_DATA_DIR=~/.p2h-web-ui/backend_data
P2H_RUNNER_IMAGE=$runner_image
P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE
P2H_APT_MIRROR=$APT_MIRROR
P2H_APT_SECURITY_MIRROR=$APT_SECURITY_MIRROR
P2H_MAX_UPLOAD_BYTES=536870912
P2H_MAX_REQUEST_BODY_BYTES=553648128
P2H_READINESS_TIMEOUT_SECONDS=5
P2H_ALLOWED_HOSTS=*
P2H_ALLOWED_ORIGINS=*
P2H_JOB_TIMEOUT_SECONDS=7200
P2H_JOB_IDLE_TIMEOUT_SECONDS=300
P2H_JOB_STAGE_TIMEOUT_SECONDS=1800
P2H_JOB_PROBLEM_TIMEOUT_SECONDS=1200
P2H_JOB_TTL_SECONDS=86400
P2H_MAX_CONCURRENT_JOBS=2
P2H_MAX_STORED_JOBS=100
P2H_MAX_STORAGE_BYTES=10737418240
P2H_MAX_LOG_BYTES=10485760
P2H_DOCKER_MEMORY=auto
P2H_DOCKER_CPUS=auto
P2H_DOALL_JOBS=auto
P2H_DOCKER_PIDS_LIMIT=1024
P2H_DOCKER_WINE_PIDS_LIMIT=4096
P2H_DOCKER_WINE_HOME_SIZE=4g
P2H_DOCKER_TMP_SIZE=512m
P2H_DOCKER_WORK_SIZE=1g
P2H_DOCKER_OUTPUT_SIZE=1g

P2H_BACKEND_HOST=$BIND_ADDRESS
P2H_BACKEND_PORT=11451
P2H_FRONTEND_HOST=$BIND_ADDRESS
P2H_FRONTEND_PORT=11452
EOF
  mv -f "$temp_file" "$env_file"
  chmod 600 "$env_file"
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
    return
  fi
  require_cmd sudo
  sudo "$@"
}

systemd_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  # A custom unit directory is used for staging and tests; otherwise require a
  # Linux host that actually booted with systemd.
  if [[ -n "${P2H_SYSTEMD_UNIT_DIR:-}" ]]; then
    return 0
  fi
  is_linux && [[ -d /run/systemd/system ]]
}

systemd_units_present() {
  [[ -f "$SYSTEMD_UNIT_DIR/$BACKEND_UNIT" || -f "$SYSTEMD_UNIT_DIR/$FRONTEND_UNIT" ]]
}

# True only when the installed units run this checkout, so another checkout's
# services are never refreshed or removed by mistake.
systemd_units_installed() {
  grep -Fxqs "WorkingDirectory=$ROOT_DIR/backend" "$SYSTEMD_UNIT_DIR/$BACKEND_UNIT" \
    || grep -Fxqs "WorkingDirectory=$ROOT_DIR" "$SYSTEMD_UNIT_DIR/$FRONTEND_UNIT"
}

config_value() {
  local key="$1"
  local line=""
  local value=""
  [[ -f "$CONFIG_FILE" ]] || return 1
  line="$(grep -E "^${key}=" "$CONFIG_FILE" | tail -n 1)" || return 1
  value="${line#*=}"
  if [[ ${#value} -ge 2 && ( "$value" == \'*\' || "$value" == \"*\" ) ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

absolute_path() {
  local path="$1"
  local directory=""
  directory="$(cd "$(dirname "$path")" && pwd)" || return 1
  printf '%s/%s' "$directory" "$(basename "$path")"
}

resolved_data_dir() {
  local value=""
  value="$(config_value P2H_DATA_DIR || true)"
  value="${value:-~/.p2h-web-ui/backend_data}"
  if [[ "$value" == "~" ]]; then
    value="$HOME"
  elif [[ "$value" == \~/* ]]; then
    value="$HOME/${value#\~/}"
  elif [[ "$value" != /* ]]; then
    # The backend resolves relative paths from its working directory.
    value="$ROOT_DIR/backend/$value"
  fi
  printf '%s' "${value%/}"
}

validate_unit_path() {
  local label="$1"
  local value="$2"
  case "$value" in
    /*) ;;
    *) die "$label must be an absolute path for systemd: $value" ;;
  esac
  case "$value" in
    *[[:space:]]*|*\"*|*\'*|*\\*|*%*|*\$*)
      die "$label contains spaces or characters systemd cannot use safely: $value"
      ;;
  esac
}

render_unit() {
  local template="$1"
  local output="$2"
  shift 2
  "$PYTHON_BIN" - "$template" "$output" "$@" <<'PY'
import re
import sys

template, output, *pairs = sys.argv[1:]
with open(template, encoding="utf-8") as handle:
    text = handle.read()
for pair in pairs:
    key, value = pair.split("=", 1)
    text = text.replace(f"@{key}@", value)
leftover = re.findall(r"@[A-Z_]+@", text)
if leftover:
    raise SystemExit(f"unrendered placeholders in {template}: {sorted(set(leftover))}")
with open(output, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
}

install_systemd_services() {
  local config_path=""
  local data_dir=""
  local node_bin=""
  local temp_dir=""

  log "Installing systemd services: $BACKEND_UNIT, $FRONTEND_UNIT"
  systemd_available || die "systemd services require Linux with a running systemd"
  [[ -x "$ROOT_DIR/backend/.venv/bin/uvicorn" ]] \
    || die "backend/.venv is missing; install the backend before enabling systemd services"
  [[ -f "$ROOT_DIR/frontend/dist/index.html" ]] \
    || die "frontend/dist is missing; build the frontend before enabling systemd services"
  [[ -f "$CONFIG_FILE" ]] || die "configuration $CONFIG_FILE is missing"
  node_bin="$(command -v node)" || die "node is required by the frontend service"
  check_python
  if [[ "$(id -u)" -eq 0 ]]; then
    warn "Installing services as root; they will run as root. Prefer a normal user with sudo."
  fi

  if systemd_units_present && ! systemd_units_installed; then
    warn "Replacing $BACKEND_UNIT/$FRONTEND_UNIT that were installed from another checkout"
  fi

  config_path="$(absolute_path "$CONFIG_FILE")"
  data_dir="$(resolved_data_dir)"
  mkdir -p "$data_dir"
  validate_unit_path "Project directory" "$ROOT_DIR"
  validate_unit_path "Configuration file" "$config_path"
  validate_unit_path "Data directory" "$data_dir"
  validate_unit_path "Node.js executable" "$node_bin"

  temp_dir="$(mktemp -d)"
  render_unit "$ROOT_DIR/deploy/systemd/$BACKEND_UNIT.in" "$temp_dir/$BACKEND_UNIT" \
    "ROOT_DIR=$ROOT_DIR" "ENV_FILE=$config_path" "DATA_DIR=$data_dir" \
    "SERVICE_USER=$SERVICE_USER" "SERVICE_GROUP=$SERVICE_GROUP"
  render_unit "$ROOT_DIR/deploy/systemd/$FRONTEND_UNIT.in" "$temp_dir/$FRONTEND_UNIT" \
    "ROOT_DIR=$ROOT_DIR" "ENV_FILE=$config_path" "NODE_BIN=$node_bin" \
    "SERVICE_USER=$SERVICE_USER" "SERVICE_GROUP=$SERVICE_GROUP"

  run_privileged mkdir -p "$SYSTEMD_UNIT_DIR"
  run_privileged install -m 0644 "$temp_dir/$BACKEND_UNIT" "$SYSTEMD_UNIT_DIR/$BACKEND_UNIT"
  run_privileged install -m 0644 "$temp_dir/$FRONTEND_UNIT" "$SYSTEMD_UNIT_DIR/$FRONTEND_UNIT"
  rm -rf "$temp_dir"

  run_privileged systemctl daemon-reload
  run_privileged systemctl enable "$BACKEND_UNIT" "$FRONTEND_UNIT"
  run_privileged systemctl restart "$BACKEND_UNIT" "$FRONTEND_UNIT"
  SYSTEMD_ACTIVE=1
}

remove_systemd_services() {
  if ! systemd_units_installed; then
    if systemd_units_present; then
      warn "$BACKEND_UNIT/$FRONTEND_UNIT belong to another checkout and were left unchanged"
    fi
    return
  fi
  log "Removing systemd services: $BACKEND_UNIT, $FRONTEND_UNIT"
  if command -v systemctl >/dev/null 2>&1; then
    run_privileged systemctl disable --now "$BACKEND_UNIT" "$FRONTEND_UNIT" \
      || warn "systemctl could not stop or disable every service; continuing"
  fi
  run_privileged rm -f "$SYSTEMD_UNIT_DIR/$BACKEND_UNIT" "$SYSTEMD_UNIT_DIR/$FRONTEND_UNIT"
  if command -v systemctl >/dev/null 2>&1; then
    run_privileged systemctl daemon-reload || true
  fi
}

refresh_runner() {
  case "$RUNNER_SOURCE" in
    prebuilt) pull_runner ;;
    build) build_runner ;;
    skip) log "Skipping runner image setup" ;;
    *) die "invalid runner source: $RUNNER_SOURCE" ;;
  esac
}

print_access_summary() {
  local bind=""
  bind="$(config_value P2H_FRONTEND_HOST || true)"
  bind="${bind:-$BIND_ADDRESS}"

  if [[ "$SYSTEMD_ACTIVE" -eq 1 ]]; then
    cat <<EOF
The Web UI runs in the background as systemd services and starts on boot:
  sudo systemctl status $BACKEND_UNIT $FRONTEND_UNIT
  sudo journalctl -u $BACKEND_UNIT -u $FRONTEND_UNIT -f
  sudo systemctl restart $BACKEND_UNIT $FRONTEND_UNIT
EOF
  else
    cat <<'EOF'
Start the Web UI:
  ./scripts/start.sh
EOF
  fi

  cat <<'EOF'

Open:
  http://127.0.0.1:11452
EOF
  if [[ "$bind" == 0.0.0.0 ]]; then
    printf '  http://<this-machine-LAN-IP>:11452   (teammates on the same network)\n'
    warn "Listening on 0.0.0.0 without any login. Only expose ports 11451/11452 to a trusted LAN or VPN."
  fi

  cat <<'EOF'

Update:     ./install.sh update
Uninstall:  ./install.sh uninstall
EOF
}

cmd_install() {
  SYSTEMD_ACTIVE=0
  log "Installing Problem Change Center Web UI"
  configure_install

  if [[ "$INSTALL_BACKEND" -eq 1 ]]; then
    install_backend
  fi
  if [[ "$INSTALL_FRONTEND" -eq 1 ]]; then
    install_frontend
  fi
  refresh_runner
  write_env_file

  if [[ "$SYSTEMD_MODE" == on ]]; then
    install_systemd_services
  elif systemd_units_installed && systemd_available; then
    if [[ "$SYSTEMD_MODE" == off ]]; then
      warn "systemd services are already installed and were left unchanged; remove them with ./install.sh uninstall"
    else
      log "Refreshing the installed systemd services"
      install_systemd_services
    fi
  fi

  printf '\nInstall complete.\n\n'
  print_access_summary
}

update_source() {
  local before=""
  local after=""
  local upstream=""

  if ! command -v git >/dev/null 2>&1 \
    || ! git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    warn "$ROOT_DIR is not a git checkout; skipping the source update"
    return
  fi
  if ! upstream="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
    warn "The current branch has no upstream; skipping git pull"
    return
  fi
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
    die "the checkout has uncommitted changes; commit or stash them, or rerun with --no-pull"
  fi

  before="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  log "Pulling the latest source from $upstream"
  git -C "$ROOT_DIR" pull --ff-only
  after="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  if [[ "$before" == "$after" ]]; then
    log "Source is already up to date"
    return
  fi

  git -C "$ROOT_DIR" log --oneline "$before..$after" | head -n 20
  log "Continuing with the updated installer"
  exec bash "$ROOT_DIR/scripts/install.sh" update --no-pull \
    ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"}
}

configure_update_runner() {
  local configured_image=""
  local value=""

  if [[ -z "$APT_MIRROR" ]]; then
    APT_MIRROR="$(config_value P2H_APT_MIRROR || true)"
  fi
  if [[ -z "$APT_SECURITY_MIRROR" ]]; then
    APT_SECURITY_MIRROR="$(config_value P2H_APT_SECURITY_MIRROR || true)"
  fi
  if [[ "$PYTHON_BASE_IMAGE_EXPLICIT" -eq 0 ]]; then
    value="$(config_value P2H_PYTHON_BASE_IMAGE || true)"
    if [[ -n "$value" ]]; then
      PYTHON_BASE_IMAGE="$value"
      PYTHON_BASE_IMAGE_EXPLICIT=1
    fi
  fi

  if [[ "$RUNNER_SOURCE_EXPLICIT" -eq 1 ]]; then
    if [[ "$RUNNER_SOURCE" == prebuilt && -z "$RUNNER_IMAGE_OVERRIDE" ]]; then
      if [[ "$BUILD_WINE" -eq 1 ]]; then
        RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_WINE_IMAGE"
      else
        RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_RUNNER_IMAGE"
      fi
    fi
    return
  fi

  configured_image="$(config_value P2H_RUNNER_IMAGE || true)"
  configured_image="${configured_image:-p2h-runner}"
  case "$configured_image" in
    p2h-runner|p2h-runner:latest)
      RUNNER_SOURCE=build
      [[ "$WINE_EXPLICIT" -eq 1 ]] || BUILD_WINE=0
      ;;
    p2h-runner-wine|p2h-runner-wine:latest)
      RUNNER_SOURCE=build
      [[ "$WINE_EXPLICIT" -eq 1 ]] || BUILD_WINE=1
      ;;
    */*)
      RUNNER_SOURCE=prebuilt
      RUNNER_IMAGE_OVERRIDE="$configured_image"
      ;;
    *)
      warn "Runner image $configured_image is a custom local image; it is not rebuilt automatically"
      RUNNER_SOURCE=skip
      ;;
  esac
}

cmd_update() {
  SYSTEMD_ACTIVE=0
  log "Updating Problem Change Center Web UI"
  [[ -f "$CONFIG_FILE" ]] \
    || die "configuration $CONFIG_FILE was not found; run ./install.sh first or pass --config"

  if [[ "$GIT_PULL" -eq 1 ]]; then
    update_source
  fi
  configure_update_runner

  if [[ "$INSTALL_BACKEND" -eq 1 ]]; then
    install_backend
  fi
  if [[ "$INSTALL_FRONTEND" -eq 1 ]]; then
    install_frontend
  fi
  refresh_runner
  write_env_file

  if [[ "$SYSTEMD_MODE" == on ]] || { [[ "$SYSTEMD_MODE" == auto ]] && systemd_units_installed; }; then
    install_systemd_services
  fi

  printf '\nUpdate complete.\n\n'
  if [[ "$SYSTEMD_ACTIVE" -eq 0 ]]; then
    printf 'If ./scripts/start.sh is running, stop it with Ctrl+C and start it again.\n\n'
  fi
  print_access_summary
}

companion_runner_image() {
  local reference="$1"
  local basename="${reference##*/}"
  local suffix=""
  [[ "$reference" != *@* ]] || return 1
  if [[ "$basename" == *:* ]]; then
    suffix=":${basename##*:}"
    reference="${reference%:*}"
  fi
  [[ "$reference" == *-wine ]] || return 1
  printf '%s%s' "${reference%-wine}" "$suffix"
}

remove_runner_images() {
  local configured=""
  local companion=""
  local image=""
  local images=(p2h-runner p2h-runner-wine)

  if ! command -v docker >/dev/null 2>&1; then
    warn "docker is not available; runner images were not removed"
    return
  fi
  configured="$(config_value P2H_RUNNER_IMAGE || true)"
  if [[ -n "$configured" ]]; then
    images+=("$configured")
    if companion="$(companion_runner_image "$configured")"; then
      images+=("$companion")
    fi
  fi
  for image in "${images[@]}"; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      log "Removing Docker image: $image"
      docker image rm "$image" >/dev/null || warn "could not remove Docker image $image"
    fi
  done
}

check_data_dir_removable() {
  local data_dir="$1"
  case "$data_dir" in
    ""|/|"$HOME"|"$ROOT_DIR"|"$ROOT_DIR/backend"|"$ROOT_DIR/frontend")
      die "refusing to delete unsafe data directory: ${data_dir:-/}"
      ;;
  esac
}

remove_data_dir() {
  local data_dir="$1"
  check_data_dir_removable "$data_dir"
  if [[ -e "$data_dir" ]]; then
    log "Deleting data directory: $data_dir"
    rm -rf "$data_dir" || warn "could not delete every file in $data_dir; remove it manually"
  fi
}

cmd_uninstall() {
  local confirmed=0
  local data_dir=""

  resolve_interactive_mode
  if [[ "$PURGE" -eq 1 ]]; then
    data_dir="$(resolved_data_dir)"
    check_data_dir_removable "$data_dir"
  fi

  printf 'This will remove:\n' >&2
  if systemd_units_installed; then
    printf '  - systemd services %s and %s\n' "$BACKEND_UNIT" "$FRONTEND_UNIT" >&2
  fi
  printf '  - backend/.venv, frontend/node_modules and frontend/dist\n' >&2
  if [[ "$PURGE" -eq 1 ]]; then
    printf '  - configuration %s\n' "$CONFIG_FILE" >&2
    printf '  - data directory %s (all jobs, uploads, logs and outputs)\n' "$data_dir" >&2
  fi
  if [[ "$REMOVE_IMAGES" -eq 1 ]]; then
    printf '  - Docker runner images\n' >&2
  fi
  printf 'The source checkout %s is kept.\n' "$ROOT_DIR" >&2

  if [[ "$ASSUME_YES" -eq 0 ]]; then
    if [[ "$INTERACTIVE_ENABLED" -eq 0 ]]; then
      die "uninstall needs confirmation; rerun with --yes"
    fi
    prompt_yes_no confirmed "Continue" n
    [[ "$confirmed" -eq 1 ]] || die "uninstall cancelled"
  fi

  remove_systemd_services
  log "Removing installed dependencies and build output"
  rm -rf "$ROOT_DIR/backend/.venv" "$ROOT_DIR/frontend/node_modules" "$ROOT_DIR/frontend/dist"
  if [[ "$REMOVE_IMAGES" -eq 1 ]]; then
    remove_runner_images
  fi
  if [[ "$PURGE" -eq 1 ]]; then
    remove_data_dir "$data_dir"
    log "Deleting configuration: $CONFIG_FILE"
    rm -f "$CONFIG_FILE"
  fi

  printf '\nUninstall complete.\n'
  if [[ "$PURGE" -eq 0 ]]; then
    printf 'Configuration and job data were kept; use --purge to delete them.\n'
  fi
  printf 'Delete %s yourself if you no longer need the source code.\n' "$ROOT_DIR"
}

case "$COMMAND" in
  install) cmd_install ;;
  update) cmd_update ;;
  uninstall) cmd_uninstall ;;
  *) die "unknown command: $COMMAND" ;;
esac
