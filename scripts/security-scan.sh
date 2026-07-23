#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
vex_file="$project_root/security/openvex.json"
scanner_image="${GRYPE_IMAGE:-anchore/grype:latest}"

for runner_image in p2h-runner:latest p2h-runner-wine:latest; do
  "$project_root/scripts/test-runner-isolation.sh" "$runner_image"
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$vex_file:/openvex.json:ro" \
    "$scanner_image" "$runner_image" \
    --vex /openvex.json \
    --only-fixed \
    --fail-on high
done
