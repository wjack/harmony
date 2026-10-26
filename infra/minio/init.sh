#!/bin/sh
# Creates the app's bucket ($S3_BUCKET) once the local MinIO server is live, then
# writes $BUCKET_READY_FILE, which the healthcheck requires. Idempotent: entrypoint.sh
# runs it on every server start.
#
# Requests are signed with curl's built-in SigV4. The root credentials reach curl as a
# config file on stdin (-K -), never as command-line arguments visible in `ps`.
set -eu

endpoint="http://127.0.0.1:9000"
bucket="${S3_BUCKET:-harmony}"
region="${MINIO_REGION:-us-east-1}"
timeout_s="${MINIO_INIT_TIMEOUT_S:-120}"

log() { echo "init.sh: $*" >&2; }

# Escapes backslashes and double quotes for a quoted value in a curl config file.
quote() { printf '%s' "$1" | sed 's/[\\"]/\\&/g'; }

user_line="user = \"$(quote "${MINIO_ROOT_USER:-minioadmin}"):$(quote "${MINIO_ROOT_PASSWORD:-minioadmin}")\""

# Prints the HTTP status of a signed request to the bucket; extra args select the method.
bucket_request() {
    printf '%s\n' "$user_line" | curl -sS -K - --aws-sigv4 "aws:amz:${region}:s3" \
        -o /dev/null -w '%{http_code}' "$@" "${endpoint}/${bucket}"
}

deadline=$(( $(date +%s) + timeout_s ))
until curl -fsS -o /dev/null "${endpoint}/minio/health/live" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "MinIO was not live after ${timeout_s}s; bucket ${bucket} not checked"
        exit 1
    fi
    sleep 1
done

status="$(bucket_request --head)"
case "$status" in
    200)
        log "bucket ${bucket} exists"
        ;;
    404)
        status="$(bucket_request -X PUT)"
        case "$status" in
            # 409: another starter created it first (BucketAlreadyOwnedByYou).
            200 | 409) log "created bucket ${bucket}" ;;
            *) log "creating bucket ${bucket} failed: HTTP ${status}"; exit 1 ;;
        esac
        ;;
    *)
        log "checking bucket ${bucket} failed: HTTP ${status} (403 means bad root credentials)"
        exit 1
        ;;
esac

touch "${BUCKET_READY_FILE}"
