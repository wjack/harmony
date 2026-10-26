# MinIO built from source (ARCHITECTURE.md §13). Compose (profile "storage") and the
# Render private service run this same image.
#
# MinIO Community Edition no longer publishes images, so we build its final release,
# RELEASE.2025-10-15T17-29-55Z. Verified 2026-10-25:
#   - `git ls-remote` resolves the tag (a lightweight tag) to commit 9e49d5e7a648.
#   - The fix for CVE-2025-62506 (GHSA-jjjj-jwhf-8rgr, session-policy bypass for service
#     accounts and STS) is commit c1a49490c78e, the tag commit's parent.

# go.mod at the tag says `go 1.24.0` and `toolchain go1.24.8`. We build with Go 1.24.13,
# the last 1.24 patch release: it satisfies both lines and adds the standard library's
# later security fixes. GOTOOLCHAIN=local stops `go` from fetching a different toolchain.
ARG GO_IMAGE=golang:1.24.13-trixie@sha256:5835f052b784aa39f2fe9070def3568605c8bc3fcd810f10402066348b61e716
ARG RUNTIME_IMAGE=debian:trixie-20261020-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
ARG MINIO_RELEASE_TAG=RELEASE.2025-10-15T17-29-55Z
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a

FROM ${GO_IMAGE} AS build

ARG MINIO_RELEASE_TAG
ARG MINIO_COMMIT
ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local

WORKDIR /src

# Fail the build if the tag ever points somewhere else.
RUN git -c advice.detachedHead=false clone --quiet --depth 1 \
        --branch "${MINIO_RELEASE_TAG}" https://github.com/minio/minio.git . \
 && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"

# Upstream's `make build` flags. gen-ldflags takes the release time with colons and,
# with MINIO_RELEASE=RELEASE, stamps the real release tag instead of DEVELOPMENT.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    release_time="$(echo "${MINIO_RELEASE_TAG#RELEASE.}" \
        | sed -E 's/T([0-9]{2})-([0-9]{2})-([0-9]{2})Z$/T\1:\2:\3Z/')" \
 && ldflags="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "${release_time}")" \
 && go build -tags kqueue -trimpath -ldflags "${ldflags}" -o /out/minio . \
 && /out/minio --version

FROM ${RUNTIME_IMAGE}

ARG MINIO_RELEASE_TAG
ARG MINIO_COMMIT
LABEL org.opencontainers.image.title="harmony-minio" \
      org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.version="${MINIO_RELEASE_TAG}" \
      org.opencontainers.image.revision="${MINIO_COMMIT}" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# curl: the healthcheck, and init.sh's bucket creation (SigV4 signing is built into
# curl, so the image needs no `mc` client). tini: PID 1, which forwards signals to
# minio and reaps init.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 minio \
 && useradd --system --uid 10001 --gid minio --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin minio \
 && mkdir /data \
 && chown minio:minio /data

COPY --from=build /out/minio /usr/local/bin/minio
COPY --chmod=0755 entrypoint.sh init.sh /usr/local/bin/

# A fresh named volume copies /data's ownership from the image, so the non-root user
# can write it. S3_BUCKET matches the app setting of the same name.
ENV MINIO_UPDATE=off \
    S3_BUCKET=harmony \
    BUCKET_READY_FILE=/tmp/harmony-bucket-ready
USER 10001:10001
VOLUME ["/data"]
EXPOSE 9000 9001

# Healthy once the server is live and init.sh has made sure the bucket exists.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:9000/minio/health/live \
        && test -f "${BUCKET_READY_FILE}"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["server", "/data", "--address", ":9000", "--console-address", ":9001"]
