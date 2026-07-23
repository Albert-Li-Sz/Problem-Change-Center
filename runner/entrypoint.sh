#!/usr/bin/env bash
set -euo pipefail

run_converter() {
  local -a drop_privileges=(
    setpriv
    --reuid=10001
    --regid=10001
    --clear-groups
    --bounding-set=-all
    --inh-caps=-all
    --ambient-caps=-all
    --no-new-privs
  )

  if mountpoint -q /result; then
    # The converter may execute an uploaded Polygon doall.sh. Give it a fresh
    # mount and PID namespace, hide the host result bind mount, then discard
    # every capability before Python starts. A trusted PID 1 supervisor reaps
    # the converter and kills any background descendants before the result is
    # copied. Avoid unshare --kill-child because its pidfd syscall is not
    # available under Wine's amd64 emulation on some arm64 Docker hosts.
    # The single-quoted program is evaluated by the namespace's shell.
    # shellcheck disable=SC2016
    unshare \
      --mount \
      --pid \
      --fork \
      --mount-proc \
      --propagation private \
      bash -c '
        set -u
        umount /result
        if mountpoint -q /result; then
          printf "error: failed to isolate result mount\n" >&2
          exit 1
        fi

        "$@" &
        converter_pid=$!
        forward_signal() {
          kill -TERM "$converter_pid" 2>/dev/null || true
        }
        trap forward_signal INT TERM HUP

        converter_status=0
        wait "$converter_pid" || converter_status=$?
        trap - INT TERM HUP

        # PID 1 is not included in kill(-1). Terminate anything the converter
        # left behind before this namespace and its private mounts disappear.
        kill -TERM -1 2>/dev/null || true
        kill -KILL -1 2>/dev/null || true
        wait 2>/dev/null || true
        exit "$converter_status"
      ' sandbox "${drop_privileges[@]}" python /opt/p2h_safe.py "$@"
    return
  fi

  "${drop_privileges[@]}" python /opt/p2h_safe.py "$@"
}

runner_status=0
run_converter "$@" || runner_status=$?

if [[ -d /result ]]; then
  if [[ "$runner_status" -eq 0 ]]; then
    if find /output -type l -print -quit | grep -q .; then
      printf 'error: converter output contains a symbolic link\n' >&2
      exit 1
    fi
    cp -a /output/. /result/
  elif [[ -f /output/.p2h-report.json && ! -L /output/.p2h-report.json ]]; then
    # Failed matrix conversions still expose their structured diagnostics. Do
    # not copy any other output: writers may have left incomplete artifacts.
    cp -- /output/.p2h-report.json /result/.p2h-report.json
  fi
fi

exit "$runner_status"
