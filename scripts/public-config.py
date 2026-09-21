#!/usr/bin/env python3
"""Generate a private, non-overwriting Compose environment for the public site."""

import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".env.public")
    args = parser.parse_args()
    template = Path(__file__).resolve().parents[1] / "deploy/public/env.example"
    value = template.read_text()
    for key in ("OWNER", "API", "WORKER"):
        value = value.replace(
            f"PUBLIC_{key}_DB_PASSWORD=GENERATE_WITH_PUBLIC_CONFIG",
            f"PUBLIC_{key}_DB_PASSWORD={secrets.token_hex(32)}",
        )
    socket = Path("/var/run/docker.sock")
    if socket.exists():
        value = value.replace("DOCKER_GID=0", f"DOCKER_GID={socket.stat().st_gid}")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(value)
    print(
        f"Created {args.output} (0600). Configure domain, SMTP and Turnstile before starting."
    )


if __name__ == "__main__":
    main()
