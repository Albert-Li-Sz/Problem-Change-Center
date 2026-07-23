#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runner_image="${1:-p2h-runner:latest}"
docker_bin="${P2H_DOCKER_BIN:-docker}"
probe_dir="$project_root/security/isolation-probe"
if [[ -n "${P2H_ISOLATION_AUDIT_ROOT:-}" ]]; then
  [[ -d "$P2H_ISOLATION_AUDIT_ROOT" && -w "$P2H_ISOLATION_AUDIT_ROOT" ]] || {
    printf 'error: P2H_ISOLATION_AUDIT_ROOT must be a writable directory\n' >&2
    exit 1
  }
  audit_dir="$(mktemp -d "$P2H_ISOLATION_AUDIT_ROOT/.p2h-isolation-audit.XXXXXXXX")"
else
  audit_dir="$(mktemp -d -t p2h-isolation-audit)"
fi

cleanup() {
  rm -r -- "$audit_dir"
}
trap cleanup EXIT

chmod 711 "$audit_dir"
mkdir "$audit_dir/result"
chmod 777 "$audit_dir/result"

"$docker_bin" run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --cap-add SYS_ADMIN \
  --cap-add SETUID \
  --cap-add SETGID \
  --cap-add SETPCAP \
  --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges:true \
  --pids-limit 32 \
  --tmpfs /work:rw,exec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=700 \
  --tmpfs /output:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=700 \
  --mount "type=bind,src=$audit_dir/result,dst=/result" \
  --mount "type=bind,src=$probe_dir,dst=/probe,readonly" \
  --env PATH=/probe:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  "$runner_image" isolation-probe

test -f "$audit_dir/result/isolation-ok.txt"
test ! -e "$audit_dir/result/direct-bypass"
test ! -e "$audit_dir/result/proc-bypass"
test ! -e "$audit_dir/result/background-survived"
grep -q '^isolated uid=10001 capabilities=none$' \
  "$audit_dir/result/isolation-ok.txt"

printf 'runner isolation probe passed: %s\n' "$runner_image"
