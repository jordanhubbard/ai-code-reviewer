#!/usr/bin/env bash
# tokenhub-start.sh — Smart TokenHub launcher
#
# Priority order (first working option wins, minimising port conflicts):
#   1. TokenHub is already responding at the target URL → no-op
#   2. A tokenhub Docker container already exists (stopped or running) → reuse it
#   3. Docker is available on Linux/macOS → start a new container
#   4. ~/Src/tokenhub Makefile is present → build binary and run it
#   5. None of the above → exit with an error and instructions
#
# Usage: scripts/tokenhub-start.sh [PORT] [HEALTHZ_URL]
#   PORT        – host port to expose (default: 8080)
#   HEALTHZ_URL – URL to poll for health (default: http://localhost:PORT/healthz)

set -euo pipefail

PORT="${1:-8080}"
HEALTHZ_URL="${2:-http://localhost:${PORT}/healthz}"
TOKENHUB_DIR="${HOME}/Src/tokenhub"
CONTAINER_NAME="tokenhub"
HEALTH_TIMEOUT=30   # seconds to wait for health after starting

# ── Colour helpers ───────────────────────────────────────────────────────────
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; NC=$'\033[0m'
info()    { echo -e "${BLUE}==>${NC} $*"; }
ok()      { echo -e "${GREEN}==>${NC} $*"; }
warn()    { echo -e "${YELLOW}==>${NC} $*"; }
die()     { echo -e "${RED}Error:${NC} $*" >&2; exit 1; }

# ── Health polling ───────────────────────────────────────────────────────────
wait_for_health() {
    local url="$1"
    local timeout="${2:-$HEALTH_TIMEOUT}"
    local i=0
    info "Waiting for TokenHub at ${url} (up to ${timeout}s)..."
    while [[ $i -lt $timeout ]]; do
        if curl -sf --max-time 2 "${url}" >/dev/null 2>&1; then
            ok "TokenHub is healthy"
            return 0
        fi
        sleep 1
        (( i++ )) || true
    done
    die "TokenHub did not become healthy within ${timeout}s at ${url}"
}

# ── Option 1: already running ─────────────────────────────────────────────────
if curl -sf --max-time 3 "${HEALTHZ_URL}" >/dev/null 2>&1; then
    ok "TokenHub already responding at ${HEALTHZ_URL} — nothing to do"
    exit 0
fi

# ── Docker availability and existing container check ─────────────────────────
DOCKER_OK=false
EXISTING_CONTAINER_ID=""

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER_OK=true
    # Check for any container named 'tokenhub' (running or stopped)
    EXISTING_CONTAINER_ID=$(docker ps -a --filter "name=^${CONTAINER_NAME}$" -q 2>/dev/null | head -1 || true)
fi

# ── Option 2: existing container (restart if stopped) ────────────────────────
if [[ -n "$EXISTING_CONTAINER_ID" ]]; then
    CONTAINER_STATUS=$(docker inspect --format '{{.State.Status}}' "$EXISTING_CONTAINER_ID" 2>/dev/null || echo "unknown")
    info "Found existing tokenhub container ${EXISTING_CONTAINER_ID} (status: ${CONTAINER_STATUS})"

    if [[ "$CONTAINER_STATUS" == "running" ]]; then
        # Container is running but health check failed — wait a bit longer
        ok "Container is already running; waiting for health..."
        wait_for_health "${HEALTHZ_URL}"
        exit 0
    fi

    # Container exists but is stopped/exited — restart it
    info "Restarting stopped tokenhub container..."
    docker start "$EXISTING_CONTAINER_ID" >/dev/null
    wait_for_health "${HEALTHZ_URL}"
    exit 0
fi

# ── Option 3: start a new container ──────────────────────────────────────────
PLATFORM=$(uname -s)
if [[ "$DOCKER_OK" == true && ( "$PLATFORM" == "Linux" || "$PLATFORM" == "Darwin" ) ]]; then
    if [[ ! -f "${TOKENHUB_DIR}/docker-compose.yaml" && ! -f "${TOKENHUB_DIR}/docker-compose.yml" ]]; then
        warn "Docker available but no docker-compose.yaml found at ${TOKENHUB_DIR}"
    else
        info "Starting new tokenhub container (port ${PORT}:8080)..."
        # Build the image from source and start the service.
        # docker compose up -d starts only the 'tokenhub' service, not optional
        # Temporal / vllm sidecars, keeping resource use minimal.
        docker compose -f "${TOKENHUB_DIR}/docker-compose.yaml" up -d --build "${CONTAINER_NAME}" \
            2>&1 | grep -v "^#" || true

        # Rename to bare 'tokenhub' if compose prefixed the project name
        ACTUAL_ID=$(docker ps --filter "name=${CONTAINER_NAME}" -q 2>/dev/null | head -1 || true)
        if [[ -n "$ACTUAL_ID" ]]; then
            wait_for_health "${HEALTHZ_URL}"
            ok "TokenHub container started (ID: ${ACTUAL_ID})"
            exit 0
        fi
        warn "Container did not start — falling through to binary mode"
    fi
fi

# ── Option 4: build and run the binary ────────────────────────────────────────
if [[ "$PLATFORM" == "Linux" || "$PLATFORM" == "Darwin" ]]; then
    if [[ ! -f "${TOKENHUB_DIR}/Makefile" ]]; then
        die "No Makefile found at ${TOKENHUB_DIR} and Docker is not available.\n" \
            "Please install Docker or clone tokenhub to ~/Src/tokenhub."
    fi

    TOKENHUB_BIN="${TOKENHUB_DIR}/bin/tokenhub"
    info "Building TokenHub binary from ${TOKENHUB_DIR}..."
    # tokenhub's Makefile build target uses Docker; fall back to direct go build
    # if Docker isn't available (binary-only path).
    if [[ "$DOCKER_OK" == true ]]; then
        make -C "${TOKENHUB_DIR}" build >/dev/null
    else
        info "Docker not available — attempting direct go build..."
        GO=${GO:-go}
        if ! command -v "$GO" >/dev/null 2>&1; then
            die "Neither Docker nor Go is available. Cannot build tokenhub binary."
        fi
        (cd "${TOKENHUB_DIR}" && CGO_ENABLED=0 "$GO" build -trimpath -o bin/tokenhub ./cmd/tokenhub)
    fi

    if [[ ! -x "$TOKENHUB_BIN" ]]; then
        die "Build completed but binary not found at ${TOKENHUB_BIN}"
    fi

    info "Starting tokenhub binary on port ${PORT}..."
    TOKENHUB_LISTEN_ADDR=":${PORT}" \
    TOKENHUB_DB_DSN="file:${HOME}/.local/share/tokenhub/tokenhub.sqlite?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)" \
        "${TOKENHUB_BIN}" &
    TOKENHUB_PID=$!
    echo "$TOKENHUB_PID" > "/tmp/tokenhub.pid"
    info "TokenHub binary started (PID ${TOKENHUB_PID})"

    wait_for_health "${HEALTHZ_URL}"
    exit 0
fi

# ── Option 5: nothing works ───────────────────────────────────────────────────
die "Cannot start TokenHub on this platform (${PLATFORM}).\n" \
    "Options:\n" \
    "  1. Start a remote TokenHub and set TOKENHUB_URL=<url> or update config.yaml\n" \
    "  2. Install Docker (Linux/macOS) and re-run\n" \
    "  3. Clone tokenhub to ~/Src/tokenhub and install Go, then re-run"
