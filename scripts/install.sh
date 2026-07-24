#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DEFAULT_PYTHON_BASE_IMAGE="python:3.14-slim-trixie"
DEFAULT_PREBUILT_RUNNER_IMAGE="ghcr.io/lmtinsuzhou/p2h-runner:main"
DEFAULT_PREBUILT_WINE_IMAGE="ghcr.io/lmtinsuzhou/p2h-runner-wine:main"
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
DEPLOYMENT_TARGET="${P2H_INSTALL_DEPLOYMENT:-}"
DEPLOYMENT_EXPLICIT=0
if [[ -n "$DEPLOYMENT_TARGET" ]]; then
  DEPLOYMENT_EXPLICIT=1
fi
SITE_ADDRESS="${P2H_SITE_ADDRESS:-}"
SITE_ORIGIN_HOST=""
ACCESS_KEY="${P2H_ACCESS_KEY:-}"
ACCESS_KEY_HASH="${P2H_ACCESS_KEY_HASH:-}"
CONFIG_FILE="${P2H_CONFIG_FILE:-$ROOT_DIR/.env}"
FORCE_CONFIG=0

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

Interactively install an internal or external deployment. When stdin is not a
terminal, the installer keeps the internal deployment defaults unless options
or environment variables select another mode.

Options:
  --interactive          Force the interactive installation wizard.
  --non-interactive      Disable all prompts.
  --deployment MODE      Select internal or external deployment.
  --internal             Alias for --deployment internal.
  --external             Alias for --deployment external.
  --wine                 Select the Wine-capable runner image.
  --no-wine              Select the normal runner without prompting.
  --runner-source MODE   Use prebuilt, build, or skip for the runner image.
  --runner-image IMAGE   Pull a prebuilt runner tag/digest instead of building.
  --skip-runner          Skip Docker runner image build.
  --skip-backend         Skip backend virtualenv and pip install.
  --skip-frontend        Skip frontend npm install.
  --no-frontend-build    Skip npm run build after installing frontend deps.
  --python PATH          Python interpreter used to create backend/.venv.
  --base-image IMAGE     Docker base image used for runner builds.
  --apt-mirror URL       Debian mirror used inside runner Docker builds.
  --apt-security URL     Debian security mirror used inside runner Docker builds.
  --build-proxy          Force passing host proxy env vars into Docker builds.
  --no-build-proxy       Do not pass host proxy env vars into Docker builds.
  --site-address HOST    Public DNS name or IP for an external deployment.
  --config PATH          Output path for the generated deployment configuration.
  --force-config         Replace an existing generated configuration file.
  -h, --help             Show this help.

Examples:
  ./install.sh
  ./install.sh --external
  P2H_ACCESS_KEY='a-long-random-key' ./install.sh --non-interactive \
    --external --site-address convert.example.com
  ./install.sh --wine
  ./install.sh --runner-image ghcr.io/lmtinsuzhou/p2h-runner:main
  ./install.sh --skip-runner
  ./install.sh --base-image registry.example.com/library/python:3.14-slim-trixie
  ./install.sh --apt-mirror https://mirrors.tuna.tsinghua.edu.cn/debian
  ./install.sh --build-proxy
  ./install.sh --no-build-proxy

If the default Docker Hub base image is unreachable, the installer will retry
with known mirror base images unless --base-image or P2H_PYTHON_BASE_IMAGE was
set explicitly. Override the retry list with P2H_PYTHON_BASE_IMAGE_FALLBACKS.

For non-interactive external deployment, provide P2H_ACCESS_KEY or an existing
P2H_ACCESS_KEY_HASH. The plaintext access key is never written to disk.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interactive)
      INTERACTIVE_MODE=on
      ;;
    --non-interactive)
      INTERACTIVE_MODE=off
      ;;
    --deployment)
      [[ $# -ge 2 ]] || die "--deployment requires internal or external"
      DEPLOYMENT_TARGET="$2"
      DEPLOYMENT_EXPLICIT=1
      shift
      ;;
    --internal)
      DEPLOYMENT_TARGET=internal
      DEPLOYMENT_EXPLICIT=1
      ;;
    --external)
      DEPLOYMENT_TARGET=external
      DEPLOYMENT_EXPLICIT=1
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
    --site-address)
      [[ $# -ge 2 ]] || die "--site-address requires a hostname"
      SITE_ADDRESS="$2"
      shift
      ;;
    --config|--production-env)
      [[ $# -ge 2 ]] || die "$1 requires a path"
      CONFIG_FILE="$2"
      shift
      ;;
    --force-config)
      FORCE_CONFIG=1
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

case "$DEPLOYMENT_TARGET" in
  ""|internal|external)
    ;;
  *)
    die "deployment mode must be internal or external"
    ;;
esac

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

prompt_secret() {
  local target="$1"
  local prompt="$2"
  local value=""

  printf '%s: ' "$prompt" >&2
  if [[ -t 0 ]]; then
    if ! IFS= read -r -s value; then
      die "interactive input ended before the access key was entered"
    fi
    printf '\n' >&2
  elif ! IFS= read -r value; then
    die "interactive input ended before the access key was entered"
  fi
  printf -v "$target" '%s' "$value"
}

normalize_site_address() {
  local values=""
  values="$($PYTHON_BIN - "$SITE_ADDRESS" <<'PY'
import ipaddress
import re
import sys

value = sys.argv[1].strip().rstrip(".").lower()
if not value or len(value) > 253 or any(character.isspace() for character in value):
    raise SystemExit(1)
if any(marker in value for marker in ("/", "@", "?", "#", "://")):
    raise SystemExit(1)
try:
    address = ipaddress.ip_address(value)
except ValueError:
    labels = value.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise SystemExit(1)
    origin_host = value
else:
    value = address.compressed
    origin_host = f"[{value}]" if address.version == 6 else value
print(f"{value}|{origin_host}")
PY
)" || return 1
  SITE_ADDRESS="${values%%|*}"
  SITE_ORIGIN_HOST="${values#*|}"
}

validate_access_key() {
  printf '%s' "$ACCESS_KEY" | "$PYTHON_BIN" -c '
import sys

value = sys.stdin.buffer.read()
if not 16 <= len(value) <= 256 or any(byte < 33 or byte > 126 for byte in value):
    raise SystemExit(1)
'
}

validate_access_key_hash() {
  # shellcheck disable=SC2016
  printf '%s' "$ACCESS_KEY_HASH" | "$PYTHON_BIN" -c '
import re
import sys

value = sys.stdin.read()
match = re.fullmatch(
    r"pbkdf2_sha256\$([0-9]{6,7})\$([0-9a-f]{32,64})\$([0-9a-f]{64})",
    value,
)
if match is None or int(match.group(1)) < 600_000:
    raise SystemExit(1)
'
}

hash_access_key() {
  # shellcheck disable=SC2016
  ACCESS_KEY_HASH="$(printf '%s' "$ACCESS_KEY" | "$PYTHON_BIN" -c '
import hashlib
import secrets
import sys

value = sys.stdin.buffer.read()
salt = secrets.token_bytes(16)
iterations = 600_000
digest = hashlib.pbkdf2_hmac("sha256", value, salt, iterations)
print(f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}")
')"
  ACCESS_KEY=""
  unset P2H_ACCESS_KEY
}

configure_install() {
  local choice=""
  local confirmed=0
  local confirmation=""

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

  if [[ "$INTERACTIVE_ENABLED" -eq 1 ]]; then
    printf '\nDeployment mode:\n  1) Internal network (no access key)\n  2) External network (access key required)\n' >&2
    if [[ "$DEPLOYMENT_EXPLICIT" -eq 0 ]]; then
      prompt_choice choice "Choose deployment mode" 1
      [[ "$choice" == 1 ]] && DEPLOYMENT_TARGET=internal || DEPLOYMENT_TARGET=external
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
  fi

  DEPLOYMENT_TARGET="${DEPLOYMENT_TARGET:-internal}"
  case "$DEPLOYMENT_TARGET" in
    internal)
      ACCESS_KEY=""
      ACCESS_KEY_HASH=""
      unset P2H_ACCESS_KEY
      ;;
    external)
      check_python
      while ! normalize_site_address; do
        if [[ "$INTERACTIVE_ENABLED" -eq 0 ]]; then
          die "--site-address must be a valid hostname or IP for external deployment"
        fi
        printf 'Public hostname or IP: ' >&2
        IFS= read -r SITE_ADDRESS || die "site address is required"
      done
      if [[ -n "$ACCESS_KEY_HASH" ]]; then
        validate_access_key_hash || die "P2H_ACCESS_KEY_HASH is not a supported PBKDF2 hash"
      else
        while [[ "$confirmed" -eq 0 ]]; do
          if [[ -z "$ACCESS_KEY" ]]; then
            if [[ "$INTERACTIVE_ENABLED" -eq 0 ]]; then
              die "P2H_ACCESS_KEY or P2H_ACCESS_KEY_HASH is required for external deployment"
            fi
            prompt_secret ACCESS_KEY "Access key (16-256 printable ASCII characters)"
          fi
          if ! validate_access_key; then
            ACCESS_KEY=""
            if [[ "$INTERACTIVE_ENABLED" -eq 0 ]]; then
              die "P2H_ACCESS_KEY must contain 16-256 printable ASCII characters without spaces"
            fi
            warn "access key must contain 16-256 printable ASCII characters without spaces"
            continue
          fi
          if [[ "$INTERACTIVE_ENABLED" -eq 1 ]]; then
            prompt_secret confirmation "Confirm access key"
            if [[ "$confirmation" != "$ACCESS_KEY" ]]; then
              ACCESS_KEY=""
              confirmation=""
              warn "access keys did not match"
              continue
            fi
          fi
          confirmed=1
        done
        hash_access_key
        confirmation=""
      fi
      ;;
  esac

  if [[ "$RUNNER_SOURCE" == prebuilt && -z "$RUNNER_IMAGE_OVERRIDE" ]]; then
    if [[ "$BUILD_WINE" -eq 1 ]]; then
      RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_WINE_IMAGE"
    else
      RUNNER_IMAGE_OVERRIDE="$DEFAULT_PREBUILT_RUNNER_IMAGE"
    fi
  fi

  if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -eq 0 ]]; then
    if [[ "$INTERACTIVE_ENABLED" -eq 1 ]]; then
      prompt_yes_no confirmed "Configuration $CONFIG_FILE exists; replace it" n
      [[ "$confirmed" -eq 1 ]] || die "configuration was not replaced"
      FORCE_CONFIG=1
    elif [[ "$DEPLOYMENT_EXPLICIT" -eq 1 ]]; then
      die "$CONFIG_FILE already exists; pass --force-config to replace it"
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
  log "Pulling prebuilt Docker runner image: $RUNNER_IMAGE_OVERRIDE"
  check_docker_access
  docker pull "$RUNNER_IMAGE_OVERRIDE"
}

write_env_file() {
  local env_file="$CONFIG_FILE"
  local runner_image="p2h-runner"
  local allowed_hosts="*"
  local allowed_origins="*"
  local access_key_hash=""
  local temp_file=""
  if [[ -n "$RUNNER_IMAGE_OVERRIDE" ]]; then
    runner_image="$RUNNER_IMAGE_OVERRIDE"
  elif [[ "$BUILD_WINE" -eq 1 ]]; then
    runner_image="p2h-runner-wine"
  fi

  if [[ -f "$env_file" && "$FORCE_CONFIG" -eq 0 ]]; then
    log "Keeping existing .env"
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
    return
  fi

  if [[ "$DEPLOYMENT_TARGET" == external ]]; then
    allowed_hosts="$SITE_ADDRESS,localhost,127.0.0.1,::1"
    allowed_origins="http://$SITE_ORIGIN_HOST:11452"
    access_key_hash="$ACCESS_KEY_HASH"
  fi

  log "Writing deployment configuration: $env_file"
  mkdir -p "$(dirname "$env_file")"
  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  chmod 600 "$temp_file"
  cat >"$temp_file" <<EOF
P2H_DEPLOYMENT_MODE=$DEPLOYMENT_TARGET
P2H_DATA_DIR=~/.p2h-web-ui/backend_data
P2H_RUNNER_IMAGE=$runner_image
P2H_PYTHON_BASE_IMAGE=$PYTHON_BASE_IMAGE
P2H_APT_MIRROR=$APT_MIRROR
P2H_APT_SECURITY_MIRROR=$APT_SECURITY_MIRROR
P2H_MAX_UPLOAD_BYTES=536870912
P2H_MAX_REQUEST_BODY_BYTES=553648128
P2H_RATE_LIMIT_REQUESTS_PER_MINUTE=240
P2H_RATE_LIMIT_UPLOADS_PER_MINUTE=12
P2H_RATE_LIMIT_AUTH_FAILURES_PER_MINUTE=10
P2H_READINESS_TIMEOUT_SECONDS=5
P2H_ALLOWED_HOSTS=$allowed_hosts
P2H_ALLOWED_ORIGINS=$allowed_origins
P2H_ACCESS_KEY_HASH='$access_key_hash'
P2H_JOB_TIMEOUT_SECONDS=7200
P2H_JOB_IDLE_TIMEOUT_SECONDS=300
P2H_JOB_STAGE_TIMEOUT_SECONDS=1800
P2H_JOB_PROBLEM_TIMEOUT_SECONDS=1200
P2H_JOB_TTL_SECONDS=86400
P2H_MAX_CONCURRENT_JOBS=2
P2H_MAX_STORED_JOBS=100
P2H_MAX_STORAGE_BYTES=10737418240
P2H_MAX_LOG_BYTES=10485760
P2H_DOCKER_MEMORY=1g
P2H_DOCKER_CPUS=2
P2H_DOCKER_PIDS_LIMIT=1024
P2H_DOCKER_WINE_PIDS_LIMIT=4096
P2H_DOCKER_WINE_HOME_SIZE=4g
P2H_DOCKER_TMP_SIZE=512m
P2H_DOCKER_WORK_SIZE=1g
P2H_DOCKER_OUTPUT_SIZE=1g

P2H_BACKEND_HOST=0.0.0.0
P2H_BACKEND_PORT=11451
P2H_FRONTEND_HOST=0.0.0.0
P2H_FRONTEND_PORT=11452
EOF
  mv -f "$temp_file" "$env_file"
  chmod 600 "$env_file"
}

main() {
  log "Installing Polygon Converter Web UI"
  configure_install

  if [[ "$INSTALL_BACKEND" -eq 1 ]]; then
    install_backend
  fi

  if [[ "$INSTALL_FRONTEND" -eq 1 ]]; then
    install_frontend
  fi

  case "$RUNNER_SOURCE" in
    prebuilt) pull_runner ;;
    build) build_runner ;;
    skip) log "Skipping runner image setup" ;;
    *) die "invalid runner source: $RUNNER_SOURCE" ;;
  esac

  write_env_file

  cat <<EOF

Install complete.

Start the Web UI:
  ./scripts/start.sh

Open:
  http://${SITE_ORIGIN_HOST:-127.0.0.1}:11452
EOF

  if [[ "$DEPLOYMENT_TARGET" == external ]]; then
    warn "External mode uses HTTP. The access key is not encrypted in transit."
    warn "Use a VPN, SSH tunnel, or your own TLS gateway for untrusted networks."
  fi
}

main
