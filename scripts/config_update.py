#!/usr/bin/env python3
"""Merge new defaults from config.yaml.defaults into config.yaml."""

import sys
from pathlib import Path

# Map of (path, old_value) -> new_value for defaults that have changed
# Format: ("section.subsection.key", old_default) -> new_default
CHANGED_DEFAULTS = {
    ("ollama.ps_monitor_interval", 5): 0,  # Changed: too verbose
}

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
    
    added, updated = merge_dicts(defaults, config)
    
    if added or updated:
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        
        msg_parts = []
        if added:
            msg_parts.append(f"{len(added)} new key(s)")
        if updated:
            msg_parts.append(f"{len(updated)} updated default(s)")
        print(f"Updated config.yaml with {', '.join(msg_parts)}")
    else:
        print("config.yaml is up to date (no changes)")

if __name__ == "__main__":
    main()
