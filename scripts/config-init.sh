#!/bin/bash
#
# config-init.sh - Interactive configuration setup for AI Code Reviewer
#
# Creates or updates config.yaml with user-specified values.
# Uses readline for editing, validates hosts with curl, and allows
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
DEFAULTS_FILE="$PROJECT_DIR/config.yaml.defaults"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Default values (from config.yaml.defaults)
DEFAULT_HOSTS=("http://localhost")
DEFAULT_MODELS=("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16" "qwen2.5-coder:32b")
DEFAULT_TIMEOUT=600
DEFAULT_MAX_TOKENS=4096
DEFAULT_TEMPERATURE="0.1"
DEFAULT_SOURCE_ROOT=".."
DEFAULT_BUILD_COMMAND="make"
DEFAULT_BUILD_TIMEOUT=7200
DEFAULT_PERSONA="personas/freebsd-angry-ai"
DEFAULT_TARGET_DIRS=10

# Arrays to hold user input
declare -a HOSTS
declare -a MODELS

# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

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
    
    # Use read with -e for readline and -i for default value
    read -e -p "$prompt [${default}]: " -i "$default" input
    
    # If empty, use default
    if [[ -z "$input" ]]; then
        input="$default"
    fi
    
    eval "$__resultvar='$input'"
}

# Read multiple values until user types 'done'
# Usage: read_array "prompt" default_array result_array
read_array() {
    local prompt="$1"
    local -n defaults=$2
    local -n result=$3
    local input
    local count=0
    
    result=()
    
    echo -e "${CYAN}$prompt${NC}"
    echo -e "  Enter values one at a time. Type ${YELLOW}done${NC} when finished."
    echo -e "  Press Enter with empty input to use the default value shown."
    echo ""
    
    while true; do
        count=$((count + 1))
        local default=""
        if [[ $count -le ${#defaults[@]} ]]; then
            default="${defaults[$((count-1))]}"
        fi
        
        if [[ -n "$default" ]]; then
            read -e -p "  [$count] " -i "$default" input
        else
            read -e -p "  [$count] (or 'done'): " input
        fi
        
        # Check for done
        if [[ "${input,,}" == "done" ]] || [[ -z "$input" && $count -gt ${#defaults[@]} ]]; then
            break
        fi
        
        # Use default if empty
        if [[ -z "$input" && -n "$default" ]]; then
            input="$default"
        fi
        
        if [[ -n "$input" ]]; then
            result+=("$input")
        fi
    done
    
    if [[ ${#result[@]} -eq 0 ]]; then
        # Use all defaults if nothing entered
        result=("${defaults[@]}")
    fi
}

# Validate a host URL by probing it
# Returns 0 if valid, 1 if invalid
validate_host() {
    local url="$1"
    local base_url="${url%/}"
    
    echo -n "  Checking $url... "
    
    # Try vLLM endpoint first
    if curl -s --connect-timeout 5 "$base_url:8000/v1/models" >/dev/null 2>&1; then
        echo -e "${GREEN}vLLM detected on :8000${NC}"
        return 0
    fi
    
    # Try Ollama endpoint
    if curl -s --connect-timeout 5 "$base_url:11434/api/tags" >/dev/null 2>&1; then
        echo -e "${GREEN}Ollama detected on :11434${NC}"
        return 0
    fi
    
    # Try with explicit port if specified
    if [[ "$url" =~ :[0-9]+$ ]]; then
        if curl -s --connect-timeout 5 "$base_url/v1/models" >/dev/null 2>&1; then
            echo -e "${GREEN}vLLM detected${NC}"
            return 0
        fi
        if curl -s --connect-timeout 5 "$base_url/api/tags" >/dev/null 2>&1; then
            echo -e "${GREEN}Ollama detected${NC}"
            return 0
        fi
    fi
    
    echo -e "${RED}not responding${NC}"
    return 1
}

# Read and validate hosts
read_hosts() {
    local -n result=$1
    local -n defaults=$2
    local temp_hosts=()
    local valid_hosts=()
    local input
    local count=0
    
    while true; do
        temp_hosts=()
        valid_hosts=()
        
        print_section "LLM Server Hosts"
        echo "Enter the URLs of your LLM servers (vLLM or Ollama)."
        echo "Just the hostname is fine - ports are auto-detected."
        echo -e "Example: ${CYAN}http://gpu-server${NC} or ${CYAN}http://192.168.1.100${NC}"
        echo ""
        echo -e "Enter hosts one at a time. Type ${YELLOW}done${NC} when finished."
        echo ""
        
        count=0
        while true; do
            count=$((count + 1))
            local default=""
            if [[ $count -le ${#defaults[@]} ]]; then
                default="${defaults[$((count-1))]}"
            fi
            
            if [[ -n "$default" ]]; then
                read -e -p "  Host [$count]: " -i "$default" input
            else
                read -e -p "  Host [$count] (or 'done'): " input
            fi
            
            if [[ "${input,,}" == "done" ]] || [[ -z "$input" && $count -gt ${#defaults[@]} ]]; then
                break
            fi
            
            if [[ -z "$input" && -n "$default" ]]; then
                input="$default"
            fi
            
            if [[ -n "$input" ]]; then
                temp_hosts+=("$input")
            fi
        done
        
        if [[ ${#temp_hosts[@]} -eq 0 ]]; then
            temp_hosts=("${defaults[@]}")
        fi
        
        # Validate each host
        echo ""
        echo "Validating hosts..."
        for host in "${temp_hosts[@]}"; do
            if validate_host "$host"; then
                valid_hosts+=("$host")
            else
                print_warning "Host $host is not responding. Include anyway? (y/N)"
                read -r include
                if [[ "${include,,}" == "y" ]]; then
                    valid_hosts+=("$host")
                    print_warning "Added $host (not validated)"
                fi
            fi
        done
        
        if [[ ${#valid_hosts[@]} -eq 0 ]]; then
            print_error "No valid hosts! At least one host is required."
            echo "Would you like to try again? (Y/n)"
            read -r retry
            if [[ "${retry,,}" == "n" ]]; then
                echo "Aborting configuration."
                exit 1
            fi
            continue
        fi
        
        break
    done
    
    result=("${valid_hosts[@]}")
}

# Load existing config values if config.yaml exists
load_existing_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "Loading existing configuration from config.yaml..."
        
        # Extract hosts (YAML array parsing with grep/sed)
        local in_hosts=0
        local in_models=0
        DEFAULT_HOSTS=()
        DEFAULT_MODELS=()
        
        while IFS= read -r line; do
            # Detect section starts
            if [[ "$line" =~ ^[[:space:]]*hosts: ]]; then
                in_hosts=1
                in_models=0
                continue
            elif [[ "$line" =~ ^[[:space:]]*models: ]]; then
                in_hosts=0
                in_models=1
                continue
            elif [[ "$line" =~ ^[[:space:]]*[a-z_]+: && ! "$line" =~ ^[[:space:]]*- ]]; then
                in_hosts=0
                in_models=0
            fi
            
            # Extract array items
            if [[ $in_hosts -eq 1 && "$line" =~ ^[[:space:]]*-[[:space:]]*[\"\']?([^\"\']+)[\"\']?$ ]]; then
                local value="${BASH_REMATCH[1]}"
                value="${value%\"}"
                value="${value#\"}"
                DEFAULT_HOSTS+=("$value")
            fi
            
            if [[ $in_models -eq 1 && "$line" =~ ^[[:space:]]*-[[:space:]]*[\"\']?([^\"\'#]+) ]]; then
                local value="${BASH_REMATCH[1]}"
                value="${value%\"}"
                value="${value#\"}"
                value="${value%%[[:space:]]*#*}"  # Remove trailing comments
                value="${value%[[:space:]]}"      # Trim trailing space
                [[ -n "$value" ]] && DEFAULT_MODELS+=("$value")
            fi
            
            # Extract scalar values
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
                DEFAULT_BUILD_COMMAND="${BASH_REMATCH[1]}"
                DEFAULT_BUILD_COMMAND="${DEFAULT_BUILD_COMMAND%\"}"
                DEFAULT_BUILD_COMMAND="${DEFAULT_BUILD_COMMAND#\"}"
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

# Display final configuration for review
display_config() {
    print_section "Configuration Summary"
    
    echo -e "${CYAN}LLM Servers:${NC}"
    for host in "${HOSTS[@]}"; do
        echo "  - $host"
    done
    echo ""
    
    echo -e "${CYAN}Models (in priority order):${NC}"
    for model in "${MODELS[@]}"; do
        echo "  - $model"
    done
    echo ""
    
    echo -e "${CYAN}LLM Settings:${NC}"
    echo "  Timeout: ${TIMEOUT}s"
    echo "  Max Tokens: $MAX_TOKENS"
    echo "  Temperature: $TEMPERATURE"
    echo ""
    
    echo -e "${CYAN}Source Configuration:${NC}"
    echo "  Root: $SOURCE_ROOT"
    echo "  Build Command: $BUILD_COMMAND"
    echo "  Build Timeout: ${BUILD_TIMEOUT}s"
    echo ""
    
    echo -e "${CYAN}Review Settings:${NC}"
    echo "  Persona: $PERSONA"
    echo "  Target Directories: $TARGET_DIRS"
    echo ""
}

# Generate the config.yaml file
generate_config() {
    local hosts_yaml=""
    for host in "${HOSTS[@]}"; do
        hosts_yaml+="    - \"$host\"\n"
    done
    
    local models_yaml=""
    for model in "${MODELS[@]}"; do
        models_yaml+="    - \"$model\"\n"
    done
    
    cat > "$CONFIG_FILE" << EOF
# AI Code Reviewer Configuration
# Generated by config-init.sh on $(date)
#
# For full documentation, see config.yaml.defaults

llm:
  hosts:
$(echo -e "$hosts_yaml" | sed 's/^/  /')
  models:
$(echo -e "$models_yaml" | sed 's/^/  /')
  timeout: $TIMEOUT
  max_tokens: $MAX_TOKENS
  temperature: $TEMPERATURE
  
  batching:
    max_parallel_requests: 0  # Dynamic (auto-detect from server)

source:
  root: "$SOURCE_ROOT"
  build_command: "$BUILD_COMMAND"
  build_timeout: $BUILD_TIMEOUT
  pre_build_command: "sudo -v"

review:
  persona: "$PERSONA"
  target_directories: $TARGET_DIRS
  max_iterations_per_directory: 200
  max_parallel_files: 0  # Dynamic (auto-detect from server)
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
    
    # Check if we should load existing config
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "Found existing config.yaml"
        load_existing_config
    else
        echo "No config.yaml found. Creating new configuration."
    fi
    
    while true; do
        # Step 1: Hosts
        read_hosts HOSTS DEFAULT_HOSTS
        
        # Step 2: Models
        print_section "LLM Models"
        echo "Enter the models to use (in priority order)."
        echo "The first available model on each host will be used."
        echo ""
        read_array "Models:" DEFAULT_MODELS MODELS
        
        # Step 3: LLM Settings
        print_section "LLM Settings"
        read_value "Request timeout (seconds)" "$DEFAULT_TIMEOUT" TIMEOUT
        read_value "Max tokens per response" "$DEFAULT_MAX_TOKENS" MAX_TOKENS
        read_value "Temperature (0.0-1.0)" "$DEFAULT_TEMPERATURE" TEMPERATURE
        
        # Step 4: Source Configuration
        print_section "Source Configuration"
        read_value "Source root directory" "$DEFAULT_SOURCE_ROOT" SOURCE_ROOT
        read_value "Build command" "$DEFAULT_BUILD_COMMAND" BUILD_COMMAND
        read_value "Build timeout (seconds)" "$DEFAULT_BUILD_TIMEOUT" BUILD_TIMEOUT
        
        # Step 5: Review Settings
        print_section "Review Settings"
        read_value "Persona directory" "$DEFAULT_PERSONA" PERSONA
        read_value "Target directories per session" "$DEFAULT_TARGET_DIRS" TARGET_DIRS
        
        # Step 6: Review and confirm
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
                print_success "Configuration saved to $CONFIG_FILE"
                echo ""
                echo "Next steps:"
                echo "  1. Review the generated config.yaml"
                echo "  2. Run 'make validate' to test the connection"
                echo "  3. Run 'make run' to start reviewing"
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

# Run main
main "$@"
