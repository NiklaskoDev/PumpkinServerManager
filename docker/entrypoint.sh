#!/bin/sh
# Container entrypoint: prepare the data folders, then run the manager as an unprivileged user.
#
# PUID/PGID follow the Unraid convention (99:100 = nobody:users) so that everything the manager and the
# Minecraft servers write is readable and editable through your network shares.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
export DATA_DIR BACKUP_DIR
export HOME="$DATA_DIR"
umask "${UMASK:-022}"

if [ "$(id -u)" != "0" ]; then
    # Started with an explicit --user: nothing to prepare, nothing to drop.
    mkdir -p "$DATA_DIR" "$BACKUP_DIR"
    exec "$@"
fi

mkdir -p "$DATA_DIR" "$BACKUP_DIR"

# A recursive chown over a large world is slow, so only repair a folder whose top level has the wrong owner
# (first start, or after changing PUID/PGID). New files are created with the right owner anyway.
for dir in "$DATA_DIR" "$BACKUP_DIR"; do
    if [ "$(stat -c '%u:%g' "$dir")" != "$PUID:$PGID" ]; then
        echo "Setting ownership of $dir to $PUID:$PGID (this can take a while on large folders)..."
        chown -R "$PUID:$PGID" "$dir"
    fi
done

exec su-exec "$PUID:$PGID" "$@"
