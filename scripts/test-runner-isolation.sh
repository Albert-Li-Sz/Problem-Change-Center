#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runner_image="${1:-p2h-runner:latest}"
probe_dir="$project_root/security/isolation-probe"
audit_dir="$(mktemp -d -t p2h-isolation-audit)"

cleanup() {
  rm -r -- "$audit_dir"
}
trap cleanup EXIT

mkdir "$audit_dir/result"
chmod 777 "$audit_dir/result"

docker run --rm \
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
