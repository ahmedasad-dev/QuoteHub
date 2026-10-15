#!/bin/sh
# One-off logical backup. PostgreSQL credentials arrive through environment only.
set -eu
umask 077

if [ ! -d /backups ] || [ ! -w /backups ]; then
    echo 'Backup directory is missing or not writable.' >&2
    exit 1
fi

case "${PGDATABASE:-}" in
    ''|*[!a-zA-Z0-9_]*)
        echo 'PGDATABASE must contain only letters, digits, or underscores.' >&2
        exit 1
        ;;
esac

stamp=$(date -u +%Y%m%dT%H%M%SZ)
partial=$(mktemp "/backups/quotehub_${PGDATABASE}_${stamp}_XXXXXX")
trap 'rm -f "$partial"' EXIT HUP INT TERM

pg_dump --no-password --format=custom --no-owner --no-acl --file="$partial"
pg_restore --list "$partial" >/dev/null
final="${partial}.dump"
mv "$partial" "$final"
(
    cd /backups
    sha256sum "$(basename "$final")" > "$(basename "$final").sha256"
)
echo "Backup created: $(basename "$final")"
