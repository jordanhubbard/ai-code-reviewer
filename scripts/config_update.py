#!/usr/bin/env python3
"""Merge new defaults from config.yaml.defaults into config.yaml."""

import sys
from pathlib import Path

def merge_dicts(defaults, config):
    """Recursively merge defaults into config, adding only new keys."""
    added = []
    for key, value in defaults.items():
        if key not in config:
            print(f"  Adding new key: {key}")
            config[key] = value
            added.append(key)
        elif isinstance(value, dict) and isinstance(config.get(key), dict):
            added.extend(merge_dicts(value, config[key]))
    return added

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
    
    added = merge_dicts(defaults, config)
    
    if added:
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f"Updated config.yaml with {len(added)} new key(s)")
    else:
        print("config.yaml is up to date (no new keys)")

if __name__ == "__main__":
    main()
