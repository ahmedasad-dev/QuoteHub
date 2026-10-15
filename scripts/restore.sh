#!/bin/sh
# Restore only into a newly created, explicitly named test/isolated database.
set -eu

target=${QUOTEHUB_RESTORE_DB:-}
archive=${QUOTEHUB_RESTORE_FILE:-}
case "$target" in
    quotehub_restore_*) ;;
    *) echo 'QUOTEHUB_RESTORE_DB must start with quotehub_restore_.' >&2; exit 1 ;;
esac
case "$target" in
    *[!a-z0-9_]*|'') echo 'QUOTEHUB_RESTORE_DB has invalid characters.' >&2; exit 1 ;;
esac
if [ "${#target}" -gt 63 ] || [ "$target" = "${QUOTEHUB_SOURCE_DB:-}" ]; then
    echo 'Restore target is invalid or is the working database.' >&2
    exit 1
fi
case "$archive" in
    quotehub_*.dump) ;;
    *) echo 'QUOTEHUB_RESTORE_FILE must name a QuoteHub .dump file.' >&2; exit 1 ;;
esac
case "$archive" in
    */*|*\\*) echo 'QUOTEHUB_RESTORE_FILE must be a filename, not a path.' >&2; exit 1 ;;
esac
if [ ! -f "/backups/$archive" ]; then
    echo 'Restore archive was not found in the backup directory.' >&2
    exit 1
fi

# createdb fails if the target already exists. No --clean or --create is used.
createdb --no-password --maintenance-db=postgres --template=template0 "$target"
if ! pg_restore --no-password --dbname="$target" --no-owner --no-acl \
    --single-transaction --exit-on-error "/backups/$archive"; then
    echo 'Restore failed. The new target database remains for inspection.' >&2
    exit 1
fi
echo "Archive restored into isolated database: $target"
