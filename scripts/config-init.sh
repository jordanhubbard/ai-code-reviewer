#!/usr/bin/env bash
#
# config-init.sh - Interactive configuration setup for AI Code Reviewer
#
# Creates or updates config.yaml with user-specified values.
# Uses readline for editing, validates the TokenHub connection, and allows
# review before saving.
#
# Usage:
#   ./scripts/config-init.sh           # Create new config
#   ./scripts/config-init.sh --update  # Update existing config
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_DIR/config.yaml"
TOKENHUB_DIR="${HOME}/Src/tokenhub"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Default values
DEFAULT_TOKENHUB_URL="http://localhost:8090"
DEFAULT_TOKENHUB_API_KEY=""
DEFAULT_TIMEOUT=600
DEFAULT_MAX_TOKENS=4096
DEFAULT_TEMPERATURE="0.1"
DEFAULT_SOURCE_ROOT=".."
DEFAULT_BUILD_COMMAND="make"
DEFAULT_BUILD_TIMEOUT=7200
DEFAULT_PERSONA="personas/freebsd-angry-ai"
DEFAULT_TARGET_DIRS=10

# Wizard output variables
TOKENHUB_URL=""
TOKENHUB_API_KEY=""

# ------------------------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------------------------

expand_path() {
    local raw="$1"
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import os,sys
p=sys.argv[1]
print(os.path.expanduser(os.path.expandvars(p)))' "$raw"
        return 0
    fi
    case "$raw" in
        ~/*)
            echo "${HOME}${raw#\~}"
            ;;
        *)
            echo "$raw"
            ;;
    esac
}

validate_source_root_dir() {
    local raw="$1"
    local expanded
    expanded=$(expand_path "$raw")
    if [[ -d "$expanded" ]]; then
        return 0
    fi
    return 1
}

print_header() {
    echo ""
    echo -e "${CYAN}============================================================${NC}"
    echo -e "${CYAN}  AI Code Reviewer - Configuration Setup${NC}"
    echo -e "${CYAN}============================================================${NC}"
    echo ""
}

print_section() {
    echo ""
    echo -e "${BLUE}--- $1 ---${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Read a single value with a default, using readline editing
# Usage: read_value "prompt" "default" result_var
read_value() {
    local prompt="$1"
    local default="$2"
    local __resultvar="$3"
    local input

    read -e -p "$prompt [${default}]: " -i "$default" input

    if [[ -z "$input" ]]; then
        input="$default"
    fi

    eval "$__resultvar='$input'"
}

# ------------------------------------------------------------------------------
# TokenHub configuration wizard
# ------------------------------------------------------------------------------

# Probe a URL's /healthz endpoint.  Returns 0 if healthy.
_probe_healthz() {
    local url="$1"
    curl -sf --max-time 5 "${url%/}/healthz" >/dev/null 2>&1
}

# Wait for /healthz to respond (used after starting a local instance)
_wait_for_healthz() {
    local url="$1"
    local timeout="${2:-30}"
    local i=0
    echo -n "  Waiting for TokenHub to start"
    while [[ $i -lt $timeout ]]; do
        if _probe_healthz "$url"; then
            echo -e " ${GREEN}ready${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
        (( i++ )) || true
    done
    echo ""
    return 1
}

# Attempt to auto-create an API key via the admin endpoint.
# Sets TOKENHUB_API_KEY on success; leaves it empty on failure.
_create_api_key() {
    local url="$1"
    local admin_token="$2"

    echo -n "  Creating API key... "
    local response
    response=$(curl -sf --max-time 10 \
        -X POST "${url%/}/admin/v1/apikeys" \
        -H "Authorization: Bearer ${admin_token}" \
        -H "Content-Type: application/json" \
        -d '{"name":"ai-code-reviewer","scopes":"[\"chat\",\"plan\"]"}' \
        2>/dev/null) || true

    if [[ -z "$response" ]]; then
        echo -e "${RED}failed (no response)${NC}"
        return 1
    fi

    # Parse .key from JSON response using python3 or grep
    local key=""
    if command -v python3 >/dev/null 2>&1; then
        key=$(printf '%s' "$response" | python3 -c \
            'import json,sys; d=json.load(sys.stdin); print(d.get("key",""))' 2>/dev/null || true)
    else
        key=$(printf '%s' "$response" | grep -o '"key"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | sed 's/"key"[[:space:]]*:[[:space:]]*"//;s/"$//' || true)
    fi

    if [[ -z "$key" ]]; then
        echo -e "${YELLOW}could not parse key from response${NC}"
        echo "  Response: $response"
        return 1
    fi

    TOKENHUB_API_KEY="$key"
    echo -e "${GREEN}created${NC}"
    return 0
}

# Prompt for an admin token and attempt to create an API key.
# On failure, leaves TOKENHUB_API_KEY empty with a helpful message.
_prompt_for_api_key() {
    local url="$1"
    echo ""
    echo -e "${CYAN}API Key Setup${NC}"
    echo "An API key scoped to 'chat' is required for the reviewer."
    echo ""
    echo "If you know your TokenHub admin token, enter it now and the wizard"
    echo "will create a key automatically.  Press Enter to skip and set the"
    echo "api_key manually in config.yaml later."
    echo ""
    read -e -p "  Admin token (or Enter to skip): " admin_token

    if [[ -z "$admin_token" ]]; then
        print_warning "Skipped. Set 'api_key' in config.yaml before running 'make run'."
        TOKENHUB_API_KEY=""
        return 0
    fi

    if _create_api_key "$url" "$admin_token"; then
        print_success "API key stored in config.yaml (store it securely — shown only once)"
    else
        print_warning "Key creation failed. Set 'api_key' in config.yaml manually."
        echo "  You can create a key at ${url}/admin or via:"
        echo "    curl -X POST ${url}/admin/v1/apikeys \\"
        echo "         -H 'Authorization: Bearer <admin_token>' \\"
        echo "         -d '{\"name\":\"ai-code-reviewer\",\"scopes\":\"[\\\"chat\\\",\\\"plan\\\"]\"}'"
        TOKENHUB_API_KEY=""
    fi
}

# Start a new tokenhub container from docker-compose.
# Returns 0 on success (healthz responds within 30s).
_start_container() {
    local port="$1"
    local compose_file="${TOKENHUB_DIR}/docker-compose.yaml"
    [[ ! -f "$compose_file" ]] && compose_file="${TOKENHUB_DIR}/docker-compose.yml"

    echo "  Building image and starting container (port ${port}:8080)..."
    echo "  This may take a few minutes on the first run."
    if ! docker compose -f "$compose_file" up -d --build tokenhub 2>&1 \
            | grep -v "^#"; then
        return 1
    fi
    _wait_for_healthz "http://localhost:${port}" 60
}

# Build and run the tokenhub binary directly.
# Returns 0 on success.
_start_binary() {
    local port="$1"
    local bin="${TOKENHUB_DIR}/bin/tokenhub"

    echo "  Building tokenhub binary (this may take a minute)..."
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        make -C "${TOKENHUB_DIR}" build >/dev/null 2>&1 || {
            print_error "make build failed"; return 1
        }
    else
        local GO="${GO:-go}"
        if ! command -v "$GO" >/dev/null 2>&1; then
            print_error "Neither Docker nor Go is available. Cannot build binary."
            return 1
        fi
        ( cd "${TOKENHUB_DIR}" && CGO_ENABLED=0 "$GO" build -trimpath \
            -o bin/tokenhub ./cmd/tokenhub ) || {
            print_error "go build failed"; return 1
        }
    fi

    [[ -x "$bin" ]] || { print_error "Binary not found at $bin"; return 1; }

    # Ensure the data directory exists
    mkdir -p "${HOME}/.local/share/tokenhub"

    echo "  Starting tokenhub binary on port ${port}..."
    TOKENHUB_LISTEN_ADDR=":${port}" \
    TOKENHUB_DB_DSN="file:${HOME}/.local/share/tokenhub/tokenhub.sqlite?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)" \
        "$bin" >/tmp/tokenhub.log 2>&1 &
    local pid=$!
    echo "$pid" > /tmp/tokenhub.pid
    echo "  TokenHub binary started (PID ${pid}, log: /tmp/tokenhub.log)"

    _wait_for_healthz "http://localhost:${port}" 30
}

configure_tokenhub() {
    print_section "TokenHub Connection"
    echo "All LLM provider selection and model routing is handled by TokenHub."
    echo "Choose how to connect to a TokenHub instance:"
    echo ""

    # ── Detect what options are available ─────────────────────────────────────
    local platform
    platform=$(uname -s)

    local docker_ok=false
    local existing_container_id=""
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        docker_ok=true
        existing_container_id=$(docker ps -a \
            --filter "name=^tokenhub$" -q 2>/dev/null | head -1 || true)
    fi

    local binary_ok=false
    if [[ ( "$platform" == "Linux" || "$platform" == "Darwin" ) && \
          -f "${TOKENHUB_DIR}/Makefile" ]]; then
        binary_ok=true
    fi

    local container_can_start=false
    if [[ "$docker_ok" == true && \
          ( "$platform" == "Linux" || "$platform" == "Darwin" ) && \
          -z "$existing_container_id" ]]; then
        container_can_start=true
    fi

    # ── Build menu ─────────────────────────────────────────────────────────────
    local opts=()
    opts+=("1" "Connect to an existing TokenHub server on the network (enter URL)")
    if [[ -n "$existing_container_id" ]]; then
        local cstatus
        cstatus=$(docker inspect --format '{{.State.Status}}' \
            "$existing_container_id" 2>/dev/null || echo "unknown")
        opts+=("2" "Reuse already-existing tokenhub container (${cstatus})")
    fi
    if [[ "$container_can_start" == true ]]; then
        opts+=("3" "Start a new tokenhub container (shareable with other apps on this machine)")
    fi
    if [[ "$binary_ok" == true ]]; then
        opts+=("4" "Build and run tokenhub binary from ~/Src/tokenhub (this machine only)")
    fi
    opts+=("5" "Skip — I will configure TokenHub manually (config.yaml) before running")

    if [[ "$platform" != "Linux" && "$platform" != "Darwin" ]]; then
        echo -e "${YELLOW}Note:${NC} Container and binary options require Linux or macOS."
        echo "  On ${platform}, only the remote server or skip options are available."
        echo ""
    fi

    # ── Present menu ───────────────────────────────────────────────────────────
    local i=0
    while [[ $i -lt ${#opts[@]} ]]; do
        echo -e "  ${CYAN}${opts[$i]}${NC}) ${opts[$((i+1))]}"
        (( i+=2 ))
    done
    echo ""

    local choice
    while true; do
        read -e -p "Choose an option [1]: " choice
        [[ -z "$choice" ]] && choice="1"

        # Validate that choice is in the menu
        local valid=false
        local j=0
        while [[ $j -lt ${#opts[@]} ]]; do
            [[ "${opts[$j]}" == "$choice" ]] && valid=true && break
            (( j+=2 ))
        done
        [[ "$valid" == true ]] && break
        echo "  Invalid choice. Options: $(echo "${opts[@]}" | tr ' ' '\n' | awk 'NR%2==1' | tr '\n' ' ')"
    done

    # ── Handle choice ──────────────────────────────────────────────────────────
    case "$choice" in

        1)  # Remote server
            echo ""
            while true; do
                read -e -p "  TokenHub URL: " -i "$DEFAULT_TOKENHUB_URL" url
                url="${url%/}"
                [[ -z "$url" ]] && { print_error "URL cannot be empty."; continue; }
                echo -n "  Verifying ${url}/healthz... "
                if _probe_healthz "$url"; then
                    echo -e "${GREEN}reachable${NC}"
                    TOKENHUB_URL="$url"
                    break
                else
                    echo -e "${RED}not reachable${NC}"
                    echo ""
                    echo -e "  Cannot connect to ${url}/healthz."
                    read -e -p "  Try a different URL? (Y/n): " retry
                    [[ "${retry,,}" == "n" ]] && break
                fi
            done
            ;;

        2)  # Existing container
            echo ""
            local cport
            cport=$(docker port "$existing_container_id" 8080/tcp 2>/dev/null \
                | grep -o ':[0-9]*' | head -1 | tr -d ':' || true)
            [[ -z "$cport" ]] && cport="8090"
            local cstatus2
            cstatus2=$(docker inspect --format '{{.State.Status}}' \
                "$existing_container_id" 2>/dev/null || echo "unknown")

            if [[ "$cstatus2" != "running" ]]; then
                echo "  Container is ${cstatus2}. Starting it..."
                docker start "$existing_container_id" >/dev/null
            fi

            if _wait_for_healthz "http://localhost:${cport}" 30; then
                TOKENHUB_URL="http://localhost:${cport}"
                print_success "Using existing container on port ${cport}"
            else
                print_error "Container did not become healthy. Try option 1 (remote) or option 3/4."
                TOKENHUB_URL=""
            fi
            ;;

        3)  # New container
            echo ""
            read -e -p "  Host port to expose (default: 8090): " -i "8090" cport
            [[ -z "$cport" ]] && cport="8090"

            if _start_container "$cport"; then
                TOKENHUB_URL="http://localhost:${cport}"
                print_success "TokenHub container running on port ${cport}"
                echo "  Other apps on this machine can use the same instance."
            else
                print_error "Failed to start container. Check Docker logs or try option 4."
                TOKENHUB_URL=""
            fi
            ;;

        4)  # Binary
            echo ""
            read -e -p "  Port to listen on (default: 8090): " -i "8090" bport
            [[ -z "$bport" ]] && bport="8090"

            if _start_binary "$bport"; then
                TOKENHUB_URL="http://localhost:${bport}"
                print_success "TokenHub binary running on port ${bport}"
            else
                print_error "Failed to start binary. Check ~/Src/tokenhub or try option 1."
                TOKENHUB_URL=""
            fi
            ;;

        5)  # Skip
            echo ""
            print_warning "TokenHub not configured. Set 'tokenhub.url' and 'tokenhub.api_key'"
            echo "  in config.yaml before running 'make run'."
            TOKENHUB_URL="$DEFAULT_TOKENHUB_URL"
            TOKENHUB_API_KEY=""
            return 0
            ;;
    esac

    # ── API key creation (if we have a reachable URL) ──────────────────────────
    if [[ -n "$TOKENHUB_URL" ]]; then
        _prompt_for_api_key "$TOKENHUB_URL"
    fi
}

# ------------------------------------------------------------------------------
# Load existing config
# ------------------------------------------------------------------------------

load_existing_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "Loading existing configuration from config.yaml..."

        while IFS= read -r line; do
            if [[ "$line" =~ ^[[:space:]]*url:[[:space:]]*[\"\']?([^\"\'#]+)[\"\']? ]]; then
                val="${BASH_REMATCH[1]}"; val="${val%[[:space:]]}";
                [[ "$val" =~ ^http ]] && DEFAULT_TOKENHUB_URL="$val"
            fi
            if [[ "$line" =~ ^[[:space:]]*api_key:[[:space:]]*[\"\']?([^\"\'#]+)[\"\']? ]]; then
                val="${BASH_REMATCH[1]}"; val="${val%[[:space:]]}";
                [[ -n "$val" ]] && DEFAULT_TOKENHUB_API_KEY="$val"
            fi
            if [[ "$line" =~ ^[[:space:]]*timeout:[[:space:]]*([0-9]+) ]]; then
                DEFAULT_TIMEOUT="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*max_tokens:[[:space:]]*([0-9]+) ]]; then
                DEFAULT_MAX_TOKENS="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*temperature:[[:space:]]*([0-9.]+) ]]; then
                DEFAULT_TEMPERATURE="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*root:[[:space:]]*[\"\']?([^\"\']+)[\"\']? ]]; then
                DEFAULT_SOURCE_ROOT="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*build_command:[[:space:]]*[\"\']?(.+)[\"\']?$ ]]; then
                val="${BASH_REMATCH[1]}"; val="${val%\"}"; val="${val#\"}";
                DEFAULT_BUILD_COMMAND="$val"
            fi
            if [[ "$line" =~ ^[[:space:]]*build_timeout:[[:space:]]*([0-9]+) ]]; then
                DEFAULT_BUILD_TIMEOUT="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*persona:[[:space:]]*[\"\']?([^\"\']+)[\"\']? ]]; then
                DEFAULT_PERSONA="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ ^[[:space:]]*target_directories:[[:space:]]*([0-9]+) ]]; then
                DEFAULT_TARGET_DIRS="${BASH_REMATCH[1]}"
            fi
        done < "$CONFIG_FILE"

        print_success "Loaded existing configuration"
    fi
}

# ------------------------------------------------------------------------------
# Display and generate config
# ------------------------------------------------------------------------------

display_config() {
    print_section "Configuration Summary"

    echo -e "${CYAN}TokenHub:${NC}"
    echo "  URL:         ${TOKENHUB_URL:-<not set>}"
    if [[ -n "$TOKENHUB_API_KEY" ]]; then
        echo "  API Key:     ${TOKENHUB_API_KEY:0:12}... (truncated)"
    else
        echo "  API Key:     <not set — configure before running>"
    fi
    echo "  Timeout:     ${TIMEOUT}s"
    echo "  Max Tokens:  $MAX_TOKENS"
    echo "  Temperature: $TEMPERATURE"
    echo ""

    echo -e "${CYAN}Source Configuration:${NC}"
    echo "  Root:          $SOURCE_ROOT"
    echo "  Build Command: $BUILD_COMMAND"
    echo "  Build Timeout: ${BUILD_TIMEOUT}s"
    echo ""

    echo -e "${CYAN}Review Settings:${NC}"
    echo "  Persona:           $PERSONA"
    echo "  Target Directories: $TARGET_DIRS"
    echo ""
}

validate_yaml() {
    local file="$1"
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "import yaml; yaml.safe_load(open('$file'))" 2>/dev/null
        return $?
    fi
    return 0
}

generate_config() {
    # Backup existing config
    if [[ -f "$CONFIG_FILE" ]]; then
        cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
        print_success "Backed up existing config to ${CONFIG_FILE}.bak"
    fi

    # Escape single quotes in values that go into YAML strings
    local esc_url="${TOKENHUB_URL//\'/\'\'}"
    local esc_key="${TOKENHUB_API_KEY//\'/\'\'}"
    local esc_src="${SOURCE_ROOT//\'/\'\'}"
    local esc_build="${BUILD_COMMAND//\'/\'\'}"
    local esc_persona="${PERSONA//\'/\'\'}"

    cat > "$CONFIG_FILE" << EOF
# AI Code Reviewer Configuration
# Generated by config-init.sh on $(date)
#
# For full documentation, see config.yaml.defaults

tokenhub:
  url: "${esc_url}"
  api_key: "${esc_key}"
  # model_hint: ""  # Optional: leave blank to let tokenhub choose
  timeout: ${TIMEOUT}
  max_tokens: ${MAX_TOKENS}
  temperature: ${TEMPERATURE}

source:
  root: "${esc_src}"
  build_command: "${esc_build}"
  build_timeout: ${BUILD_TIMEOUT}
  pre_build_command: "sudo -v"

review:
  persona: "${esc_persona}"
  target_directories: ${TARGET_DIRS}
  max_iterations_per_directory: 200
  max_parallel_files: 0
  chunk_threshold: 400
  chunk_size: 250
  skip_patterns:
    - "*.o"
    - "*.a"
    - "*.so"
    - ".git/*"
    - "ai-code-reviewer/*"
    - "*.pyc"
    - "__pycache__/*"

logging:
  log_dir: ".ai-code-reviewer/logs"
  level: "INFO"
  max_log_files: 100
EOF
}

# ------------------------------------------------------------------------------
# Main flow
# ------------------------------------------------------------------------------

main() {
    print_header

    if [[ -f "$CONFIG_FILE" ]]; then
        echo "Found existing config.yaml"
        load_existing_config
    else
        echo "No config.yaml found. Creating new configuration."
    fi

    while true; do
        # Step 1: Source Configuration
        print_section "Source Configuration"
        read_value "Source root directory" "$DEFAULT_SOURCE_ROOT" SOURCE_ROOT
        while true; do
            if validate_source_root_dir "$SOURCE_ROOT"; then
                break
            fi
            echo ""
            print_warning "Not a directory (after path expansion): $SOURCE_ROOT"
            echo ""
            echo -n "Use this value anyway? (y/N/r to re-enter): "
            read -r add_anyway
            case "${add_anyway,,}" in
                y)
                    print_warning "Keeping source root (not validated): $SOURCE_ROOT"
                    break
                    ;;
                *)
                    read_value "Source root directory" "$DEFAULT_SOURCE_ROOT" SOURCE_ROOT
                    ;;
            esac
        done

        read_value "Build command" "$DEFAULT_BUILD_COMMAND" BUILD_COMMAND
        read_value "Build timeout (seconds)" "$DEFAULT_BUILD_TIMEOUT" BUILD_TIMEOUT

        # Step 2: TokenHub connection
        configure_tokenhub

        # Step 3: TokenHub request settings
        print_section "TokenHub Request Settings"
        echo "These control how requests are formed, not which provider handles them."
        echo ""
        read_value "Request timeout (seconds)" "$DEFAULT_TIMEOUT" TIMEOUT
        read_value "Max tokens per response" "$DEFAULT_MAX_TOKENS" MAX_TOKENS
        read_value "Temperature (0.0-1.0)" "$DEFAULT_TEMPERATURE" TEMPERATURE

        # Step 4: Review Settings
        print_section "Review Settings"
        read_value "Persona directory" "$DEFAULT_PERSONA" PERSONA
        read_value "Target directories per session" "$DEFAULT_TARGET_DIRS" TARGET_DIRS

        # Step 5: Review and confirm
        display_config

        echo -e "${CYAN}What would you like to do?${NC}"
        echo "  [S]ave configuration"
        echo "  [E]dit again"
        echo "  [A]bort"
        echo ""
        read -p "Choice [S/e/a]: " choice

        case "${choice,,}" in
            s|"")
                generate_config
                echo ""

                echo -n "Validating YAML syntax... "
                if validate_yaml "$CONFIG_FILE"; then
                    echo -e "${GREEN}OK${NC}"
                    print_success "Configuration saved to $CONFIG_FILE"
                    if [[ -f "${CONFIG_FILE}.bak" ]]; then
                        echo -e "  (Previous config backed up to ${CYAN}config.yaml.bak${NC})"
                    fi
                else
                    echo -e "${RED}FAILED${NC}"
                    print_error "Generated config.yaml has invalid YAML syntax!"
                    if [[ -f "${CONFIG_FILE}.bak" ]]; then
                        echo "Restoring from backup..."
                        mv "${CONFIG_FILE}.bak" "$CONFIG_FILE"
                        print_warning "Restored previous config.yaml from backup"
                    fi
                    exit 1
                fi

                echo ""
                echo "Next steps:"
                if [[ -z "$TOKENHUB_API_KEY" ]]; then
                    echo "  1. Set 'api_key' in config.yaml (run 'make config-init' again with admin token)"
                else
                    echo "  1. API key is configured"
                fi
                echo "  2. Run 'make tokenhub-status' to confirm TokenHub is reachable"
                echo "  3. Run 'make validate' to test the full connection"
                echo "  4. Run 'make run' to start reviewing"
                echo ""
                exit 0
                ;;
            e)
                echo "Starting over..."
                continue
                ;;
            a)
                echo "Aborted. No changes made."
                exit 1
                ;;
            *)
                echo "Invalid choice. Please enter S, E, or A."
                ;;
        esac
    done
}

main "$@"
