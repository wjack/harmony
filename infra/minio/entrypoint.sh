#!/bin/sh
# Runs under tini (PID 1). Starts the bucket bootstrap beside the server, then becomes
# the minio process.
#
# The image starts as root for one reason: a Render disk is mounted at /data owned by
# root (with a root-only lost+found), which uid 10001 can't write, and MinIO then exits
# with "Unable to initialize backend: file access denied". As root this script hands
# /data to the minio user, then re-runs itself as that user (setpriv, util-linux), so
# neither MinIO nor init.sh ever runs as root. A Compose named volume already copies
# the image's minio-owned /data, so there it changes nothing.
set -eu

if [ "$(id -u)" = 0 ]; then
    if [ "$(stat -c %u /data)" != 10001 ]; then
        echo "entrypoint.sh: /data is owned by uid $(stat -c %u /data); handing it to minio (10001)" >&2
        chown -R 10001:10001 /data
    fi
    exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all \
        --no-new-privs "$0" "$@"
fi

if [ "${1:-}" = "server" ]; then
    rm -f "${BUCKET_READY_FILE}"
    # Double fork: init.sh is reparented to tini, which reaps it when it exits, and
    # minio (exec'd below) stays tini's direct child, so SIGTERM reaches it directly.
    ( /usr/local/bin/init.sh & )
fi

exec /usr/local/bin/minio "$@"
