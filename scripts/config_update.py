#!/usr/bin/env python3
"""Merge new defaults from config.yaml.defaults into config.yaml."""

import sys
from pathlib import Path

# Map of (path, old_value) -> new_value for defaults that have changed
# Format: ("section.subsection.key", old_default) -> new_default
CHANGED_DEFAULTS = {
    ("llm.timeout", 300): 600,  # Changed: prevent timeouts on large files
}


def migrate_to_llm_providers(config):
    """
    Migrate legacy config formats into the new llm.providers list.

    Handles:
      - 'tokenhub' section  (url + api_key -> single provider)
      - 'ollama' section    (legacy pre-TokenHub era)
      - 'llm' section without 'providers' key (flat url/api_key)

    Returns True if any migration was performed.
    """
    migrated = False
    llm = config.setdefault('llm', {})

    # ── tokenhub → llm.providers ─────────────────────────────────────────
    if 'tokenhub' in config:
        th = config['tokenhub']
        if 'providers' not in llm:
            url = str(th.get('url') or 'http://localhost:8090').rstrip('/')
            api_key = str(th.get('api_key') or '')
            llm['providers'] = [{'url': url, 'api_key': api_key}]

        if th.get('model_hint') and 'model' not in llm:
            llm['model'] = th['model_hint']

        for key in ('timeout', 'max_tokens', 'temperature'):
            if key in th and key not in llm:
                llm[key] = th[key]

        del config['tokenhub']
        migrated = True

    # ── ollama → llm.providers ───────────────────────────────────────────
    if 'ollama' in config:
        ollama = config['ollama']
        if 'providers' not in llm:
            url = str(ollama.get('url') or 'http://localhost:11434').rstrip('/')
            llm['providers'] = [{'url': url, 'api_key': ''}]
        for key in ('timeout', 'max_tokens', 'temperature'):
            if key in ollama and key not in llm:
                llm[key] = ollama[key]
        del config['ollama']
        migrated = True

    return migrated


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
    
    migrated = migrate_to_llm_providers(config)
    if migrated:
        print("  Migrated legacy config (tokenhub/ollama) into 'llm.providers' format")

    added, updated = merge_dicts(defaults, config)

    if added or updated or migrated:
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        msg_parts = []
        if migrated:
            msg_parts.append("provider migration")
        if added:
            msg_parts.append(f"{len(added)} new key(s)")
        if updated:
            msg_parts.append(f"{len(updated)} updated default(s)")
        print(f"Updated config.yaml with {', '.join(msg_parts)}")
    else:
        print("config.yaml is up to date (no changes)")

if __name__ == "__main__":
    main()
