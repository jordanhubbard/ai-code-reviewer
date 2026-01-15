#!/usr/bin/env python3
"""Merge new defaults from config.yaml.defaults into config.yaml."""

import sys
from pathlib import Path

# Map of (path, old_value) -> new_value for defaults that have changed
# Format: ("section.subsection.key", old_default) -> new_default
CHANGED_DEFAULTS = {
    ("ollama.ps_monitor_interval", 5): 0,  # Changed: too verbose
    ("ollama.timeout", 300): 600,  # Changed: prevent timeouts on large files
    ("llm.ps_monitor_interval", 5): 0,  # Changed: too verbose
    ("llm.timeout", 300): 600,  # Changed: prevent timeouts on large files
}


def migrate_ollama_to_llm(config):
    """
    Migrate legacy 'ollama' config section to new 'llm' format.
    
    Returns True if migration was performed.
    """
    if 'llm' in config:
        # Already has new format
        return False
    
    if 'ollama' not in config:
        # No config to migrate
        return False
    
    ollama = config['ollama']
    
    # Convert single url/model to lists
    hosts = [ollama.get('url', 'http://localhost:11434')]
    models = [ollama.get('model', 'qwen2.5-coder:32b')]
    
    # Add new preferred model if not already present
    preferred_model = 'NVIDIA-Nemotron-3-Nano-30B-A3B-BF16'
    if preferred_model not in models:
        models.insert(0, preferred_model)
    
    config['llm'] = {
        'hosts': hosts,
        'models': models,
        'timeout': ollama.get('timeout', 600),
        'max_tokens': ollama.get('max_tokens', 4096),
        'temperature': ollama.get('temperature', 0.1),
        'ps_monitor_interval': ollama.get('ps_monitor_interval', 0),
        'batching': ollama.get('batching', {}),
        'options': ollama.get('options', {}),
    }
    
    # Remove old ollama section (it's now deprecated)
    del config['ollama']
    
    return True

def merge_dicts(defaults, config, path="", added=None, updated=None):
    """Recursively merge defaults into config, adding new keys and updating changed defaults."""
    if added is None:
        added = []
    if updated is None:
        updated = []
    
    for key, value in defaults.items():
        current_path = f"{path}.{key}" if path else key
        
        if key not in config:
            print(f"  Adding new key: {current_path}")
            config[key] = value
            added.append(current_path)
        elif isinstance(value, dict) and isinstance(config.get(key), dict):
            merge_dicts(value, config[key], current_path, added, updated)
        else:
            # Check if this is a changed default that should be updated
            for (check_path, old_val), new_val in CHANGED_DEFAULTS.items():
                if current_path == check_path and config[key] == old_val and value == new_val:
                    print(f"  Updating changed default: {current_path} ({old_val} -> {new_val})")
                    config[key] = new_val
                    updated.append(current_path)
    
    return added, updated

def main():
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML not installed. Run: pip install pyyaml")
        sys.exit(1)
    
    defaults_path = Path("config.yaml.defaults")
    config_path = Path("config.yaml")
    
    if not defaults_path.exists():
        print(f"ERROR: {defaults_path} not found")
        sys.exit(1)
    
    if not config_path.exists():
        print("ERROR: config.yaml not found (use 'make config-update' to create)")
        sys.exit(1)
    
    with open(defaults_path) as f:
        defaults = yaml.safe_load(f)
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    # First, migrate ollama -> llm if needed
    migrated = migrate_ollama_to_llm(config)
    if migrated:
        print("  Migrated 'ollama' section to new 'llm' format")
        print("    - Converted url -> hosts (array)")
        print("    - Converted model -> models (array with priority fallback)")
        print("    - Added NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 as preferred model")
    
    added, updated = merge_dicts(defaults, config)
    
    if added or updated or migrated:
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        
        msg_parts = []
        if migrated:
            msg_parts.append("ollama->llm migration")
        if added:
            msg_parts.append(f"{len(added)} new key(s)")
        if updated:
            msg_parts.append(f"{len(updated)} updated default(s)")
        print(f"Updated config.yaml with {', '.join(msg_parts)}")
    else:
        print("config.yaml is up to date (no changes)")

if __name__ == "__main__":
    main()
