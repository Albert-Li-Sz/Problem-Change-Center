#!/usr/bin/env bash
set -euo pipefail
# Run daily with systemd. Keep the age decryption key off this server.
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_file="${PUBLIC_ENV_FILE:-$project_root/.env.public}"
backup_directory="/srv/p2h/backups"
: "${PUBLIC_BACKUP_RECIPIENT:?Set the age public recipient (age1...)}"
command -v age >/dev/null
install -d -m 0700 "$backup_directory"
backup_path="$backup_directory/p2h-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
temporary_backup="$(mktemp "$backup_directory/.p2h-backup.XXXXXXXX")"
trap 'rm -f -- "$temporary_backup"' EXIT
docker compose --env-file "$config_file" -f "$project_root/docker-compose.public.yml" \
  exec -T postgres pg_dump -U p2h_owner -d p2h -Fc \
  | age -r "$PUBLIC_BACKUP_RECIPIENT" -o "$temporary_backup"
chmod 0600 "$temporary_backup"
mv -- "$temporary_backup" "$backup_path"
# Only this script's dated backups in this fixed directory expire.
find /srv/p2h/backups -maxdepth 1 -type f -name 'p2h-*.dump.age' -mtime +6 -delete
printf 'Encrypted backup completed: %s\n' "$backup_path"
