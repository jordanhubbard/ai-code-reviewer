#!/usr/bin/env bash
#
# config-init.sh - Interactive configuration setup for AI Code Reviewer
#
# Creates or updates config.yaml with user-specified values.
# Uses readline for editing, validates LLM provider connections, and allows
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

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Default values
DEFAULT_TIMEOUT=600
DEFAULT_MAX_TOKENS=4096
DEFAULT_TEMPERATURE="0.1"
DEFAULT_SOURCE_ROOT=".."
DEFAULT_BUILD_COMMAND="make"
DEFAULT_BUILD_TIMEOUT=7200
DEFAULT_PERSONA="personas/freebsd-angry-ai"
DEFAULT_TARGET_DIRS=10

# Provider list: arrays of url and api_key
declare -a PROVIDER_URLS
declare -a PROVIDER_KEYS

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
# Provider probing
# ------------------------------------------------------------------------------

# Probe a URL's /v1/models endpoint (works for any OpenAI-compatible server)
_probe_provider() {
    local url="$1"
    local key="$2"
    local auth_header=""
    if [[ -n "$key" ]]; then
        auth_header="-H \"Authorization: Bearer ${key}\""
    fi
    eval curl -sf --max-time 5 $auth_header "${url%/}/v1/models" >/dev/null 2>&1
}

# Also try /healthz as a fallback (TokenHub, some custom servers)
_probe_healthz() {
    local url="$1"
    curl -sf --max-time 5 "${url%/}/healthz" >/dev/null 2>&1
}

# Probe either endpoint
_probe_any() {
    local url="$1"
    local key="$2"
    _probe_provider "$url" "$key" || _probe_healthz "$url"
}

# ------------------------------------------------------------------------------
# LLM provider configuration
# ------------------------------------------------------------------------------

configure_providers() {
    print_section "LLM Provider Configuration"
    echo "Configure one or more OpenAI-compatible LLM providers."
    echo "Any server with a /v1/chat/completions endpoint works:"
    echo "  vLLM, TokenHub, OpenAI, Ollama (OpenAI mode), llama.cpp, etc."
    echo ""
    echo "Providers are tried in order — the first healthy one is used."
    echo ""

    # Reset provider arrays
    PROVIDER_URLS=()
    PROVIDER_KEYS=()

    local adding=true
    local provider_num=1

    while [[ "$adding" == true ]]; do
        echo -e "${CYAN}Provider #${provider_num}:${NC}"

        local url=""
        local key=""
        local default_url="http://localhost:8090"
        if [[ $provider_num -eq 1 && ${#PROVIDER_URLS[@]} -eq 0 ]]; then
            default_url="${DEFAULT_PROVIDER_URL:-http://localhost:8090}"
        else
            default_url=""
        fi

        while true; do
            read -e -p "  URL: " -i "$default_url" url
            url="${url%/}"
            if [[ -z "$url" ]]; then
                if [[ $provider_num -eq 1 ]]; then
                    print_error "At least one provider URL is required."
                    continue
                else
                    adding=false
                    break
                fi
            fi

            read -e -p "  API key (Enter for none): " key
            key="${key// /}"

            echo -n "  Verifying ${url}... "
            if _probe_any "$url" "$key"; then
                echo -e "${GREEN}reachable${NC}"
            else
                echo -e "${YELLOW}not reachable (may start later)${NC}"
            fi

            PROVIDER_URLS+=("$url")
            PROVIDER_KEYS+=("$key")
            (( provider_num++ )) || true
            break
        done

        if [[ "$adding" == true ]]; then
            echo ""
            read -e -p "  Add another provider? (y/N): " more
            if [[ "${more,,}" != "y" ]]; then
                adding=false
            fi
        fi
    done

    if [[ ${#PROVIDER_URLS[@]} -eq 0 ]]; then
        print_warning "No providers configured. Add at least one in config.yaml before running."
        PROVIDER_URLS=("http://localhost:8090")
        PROVIDER_KEYS=("")
    fi
}

# ------------------------------------------------------------------------------
# Load existing config
# ------------------------------------------------------------------------------

load_existing_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "Loading existing configuration from config.yaml..."

        # Try to load providers from existing config using Python
        if command -v python3 >/dev/null 2>&1; then
            eval "$(python3 -c "
import yaml, sys
try:
    d = yaml.safe_load(open('$CONFIG_FILE'))
    # New format: llm.providers
    llm = d.get('llm') or {}
    provs = llm.get('providers') or []
    # Legacy format: tokenhub
    th = d.get('tokenhub') or {}
    if provs:
        for i, p in enumerate(provs):
            print(f'PROVIDER_URLS[{i}]=\"{p.get(\"url\",\"\")}\"')
            print(f'PROVIDER_KEYS[{i}]=\"{p.get(\"api_key\",\"\")}\"')
    elif th.get('url'):
        print(f'DEFAULT_PROVIDER_URL=\"{th[\"url\"]}\"')
        print(f'DEFAULT_PROVIDER_KEY=\"{th.get(\"api_key\",\"\")}\"')
except Exception:
    pass
" 2>/dev/null || true)"
        fi

        while IFS= read -r line; do
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

    echo -e "${CYAN}LLM Providers:${NC}"
    for i in "${!PROVIDER_URLS[@]}"; do
        local url="${PROVIDER_URLS[$i]}"
        local key="${PROVIDER_KEYS[$i]}"
        if [[ -n "$key" ]]; then
            echo "  ${i}: ${url}  (key: ${key:0:12}...)"
        else
            echo "  ${i}: ${url}  (no key)"
        fi
    done
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

    # Build providers YAML block
    local providers_block=""
    for i in "${!PROVIDER_URLS[@]}"; do
        local esc_url="${PROVIDER_URLS[$i]//\'/\'\'}"
        local esc_key="${PROVIDER_KEYS[$i]//\'/\'\'}"
        providers_block+="    - url: \"${esc_url}\""$'\n'
        providers_block+="      api_key: \"${esc_key}\""$'\n'
    done

    local esc_src="${SOURCE_ROOT//\'/\'\'}"
    local esc_build="${BUILD_COMMAND//\'/\'\'}"
    local esc_persona="${PERSONA//\'/\'\'}"

    cat > "$CONFIG_FILE" << EOF
# AI Code Reviewer Configuration
# Generated by config-init.sh on $(date)
#
# For full documentation, see config.yaml.sample

llm:
  providers:
${providers_block}  # model: ""  # Optional: leave blank to auto-discover
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

        # Step 2: LLM providers
        configure_providers

        # Step 3: Request settings
        print_section "LLM Request Settings"
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
                echo "  1. Run 'make validate' to test the LLM connection"
                echo "  2. Run 'make run' to start reviewing"
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
