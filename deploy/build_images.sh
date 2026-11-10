#!/usr/bin/env bash
# Build the gateway images at a fixed version and pin that version in .env.
#
# Deployment never builds: the compose files only name an image, read from
# .env, and `docker compose up -d` starts whatever that names. This script is
# the one place that builds. It tags both images with
#
#     <UTC date>-<git short sha>        e.g. 20261110-6d94998
#
# so an image always names the commit it came from, and writes that tag into
# deploy/gateway/.env and deploy/llm/.env (only into the ones that exist).
#
# Optionally it also saves both images to one archive, for machines that load
# the images instead of building them:
#
#     deploy/build_images.sh                           # build and pin
#     deploy/build_images.sh --save-dir /some/dir      # ...and save <dir>/llm-gateway-<ver>.tar.gz
#     IMAGE_SAVE_DIR=/some/dir deploy/build_images.sh  # the same
#     deploy/build_images.sh --allow-dirty             # build uncommitted changes; tag gets -dirty
#
#     docker load -i /some/dir/llm-gateway-<ver>.tar.gz    # on another machine
#
# Uncommitted changes under gateway/ or deploy/tests/ - the build inputs - stop
# the build unless --allow-dirty is given, because the tag would otherwise name
# a commit that does not contain the code inside the image.

set -euo pipefail

# Directory to save the image archive to. Empty: do not save one.
SAVE_DIR="${IMAGE_SAVE_DIR:-}"

GATEWAY_IMAGE_NAME="${GATEWAY_IMAGE_NAME:-llm-gateway}"
SMOKE_TESTS_IMAGE_NAME="${SMOKE_TESTS_IMAGE_NAME:-llm-gateway-smoke-tests}"
ALLOW_DIRTY=false

usage() {
    sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save-dir)
            [[ $# -ge 2 ]] || { echo "--save-dir needs a directory" >&2; exit 2; }
            SAVE_DIR="$2"
            shift 2
            ;;
        --save-dir=*)
            SAVE_DIR="${1#*=}"
            shift
            ;;
        --allow-dirty)
            ALLOW_DIRTY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

REVISION="$(git rev-parse --short=7 HEAD)"
VERSION="$(date -u +%Y%m%d)-${REVISION}"

if [[ -n "$(git status --porcelain -- gateway deploy/tests)" ]]; then
    if [[ "$ALLOW_DIRTY" != true ]]; then
        echo "Uncommitted changes under gateway/ or deploy/tests/:" >&2
        git status --short -- gateway deploy/tests >&2
        echo "Commit them, or pass --allow-dirty to build them as ${VERSION}-dirty." >&2
        exit 1
    fi

    VERSION="${VERSION}-dirty"
fi

GATEWAY_IMAGE="${GATEWAY_IMAGE_NAME}:${VERSION}"
SMOKE_TESTS_IMAGE="${SMOKE_TESTS_IMAGE_NAME}:${VERSION}"
LABELS=(
    --label "org.opencontainers.image.version=${VERSION}"
    --label "org.opencontainers.image.revision=$(git rev-parse HEAD)"
)

echo "==> [1/3] Building ${GATEWAY_IMAGE}"
docker build "${LABELS[@]}" -t "$GATEWAY_IMAGE" gateway

echo "==> [2/3] Building ${SMOKE_TESTS_IMAGE}"
docker build "${LABELS[@]}" -t "$SMOKE_TESTS_IMAGE" -f deploy/tests/Dockerfile .

# Set KEY=VALUE in an env file: replace the line if the key is there, append it
# otherwise. Written through awk and cat rather than `sed -i`, which differs
# between GNU and BSD and would replace the file's inode and permissions.
pin_env() {
    local file="$1" key="$2" value="$3" tmp

    if [[ ! -f "$file" ]]; then
        echo "    skip ${file}: not found"
        return
    fi

    tmp="$(mktemp)"
    awk -v key="$key" -v value="$value" '
        index($0, key "=") == 1 { print key "=" value; done = 1; next }
        { print }
        END { if (!done) print key "=" value }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
    echo "    ${file}: ${key}=${value}"
}

echo "==> [3/3] Pinning ${VERSION} in .env"
pin_env deploy/gateway/.env GATEWAY_IMAGE "$GATEWAY_IMAGE"
pin_env deploy/gateway/.env GATEWAY_SMOKE_TESTS_IMAGE "$SMOKE_TESTS_IMAGE"
pin_env deploy/llm/.env LLM_SMOKE_TESTS_IMAGE "$SMOKE_TESTS_IMAGE"

if [[ -z "$SAVE_DIR" ]]; then
    echo "Done: ${GATEWAY_IMAGE}, ${SMOKE_TESTS_IMAGE}. No archive saved (no --save-dir / IMAGE_SAVE_DIR)."
    exit 0
fi

mkdir -p "$SAVE_DIR"
ARCHIVE="${SAVE_DIR%/}/${GATEWAY_IMAGE_NAME}-${VERSION}.tar.gz"
PARTIAL="${ARCHIVE}.partial"

# A half-written archive must never be mistaken for a finished one, so the
# archive is written under a temporary name and renamed only once complete.
trap 'rm -f "$PARTIAL"' EXIT

echo "==> Saving both images to ${ARCHIVE}"

if command -v pv >/dev/null 2>&1; then
    # docker save streams roughly the images' unpacked size, which gives pv a
    # total for the percentage, rate and ETA.
    total=0
    for image in "$GATEWAY_IMAGE" "$SMOKE_TESTS_IMAGE"; do
        total=$(( total + $(docker image inspect -f '{{.Size}}' "$image") ))
    done

    docker save "$GATEWAY_IMAGE" "$SMOKE_TESTS_IMAGE" \
        | pv -s "$total" -N save \
        | gzip -c > "$PARTIAL"
elif dd --version >/dev/null 2>&1; then
    # GNU dd reports bytes done and the rate, without a total or an ETA.
    echo "    (install pv for a total and an ETA)"
    docker save "$GATEWAY_IMAGE" "$SMOKE_TESTS_IMAGE" \
        | dd bs=4M status=progress \
        | gzip -c > "$PARTIAL"
else
    echo "    (install pv to see a progress bar)"
    docker save "$GATEWAY_IMAGE" "$SMOKE_TESTS_IMAGE" | gzip -c > "$PARTIAL"
fi

mv "$PARTIAL" "$ARCHIVE"
trap - EXIT

echo "Done: ${GATEWAY_IMAGE}, ${SMOKE_TESTS_IMAGE}"
echo "Load on another machine with:"
echo "    docker load -i ${ARCHIVE}"
