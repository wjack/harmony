#!/bin/sh
# Runs under tini (PID 1). Starts the bucket bootstrap beside the server, then becomes
# the minio process.
set -eu

if [ "${1:-}" = "server" ]; then
    rm -f "${BUCKET_READY_FILE}"
    # Double fork: init.sh is reparented to tini, which reaps it when it exits, and
    # minio (exec'd below) stays tini's direct child, so SIGTERM reaches it directly.
    ( /usr/local/bin/init.sh & )
fi

exec /usr/local/bin/minio "$@"
