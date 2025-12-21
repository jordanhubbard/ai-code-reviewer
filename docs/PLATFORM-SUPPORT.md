# Platform Support

The AI Code Reviewer is **fully cross-platform** and automatically detects your operating system to use the appropriate package manager and installation commands.

## Supported Platforms

### FreeBSD
- **Package Manager**: `pkg`
- **Python Package**: `python3`, `py311-pip`
- **Detection**: `uname -s` returns `FreeBSD`

### macOS
- **Package Manager**: Homebrew (`brew`)
- **Python Package**: `python3` (via Homebrew)
- **pip Installation**: Uses `--break-system-packages` flag for externally-managed Python environments
- **Detection**: `uname -s` returns `Darwin`
- **Requirements**: Homebrew must be installed (https://brew.sh)

### Linux

#### Debian/Ubuntu
- **Package Manager**: `apt-get`
- **Python Packages**: `python3`, `python3-pip`
- **Detection**: Checks for `apt-get` command

#### RHEL/CentOS (older)
- **Package Manager**: `yum`
- **Python Packages**: `python3`, `python3-pip`
- **Detection**: Checks for `yum` command

#### Fedora/RHEL 8+
- **Package Manager**: `dnf`
- **Python Packages**: `python3`, `python3-pip`
- **Detection**: Checks for `dnf` command

## Automatic Dependency Installation

The `make check-deps` target:

1. **Detects your OS** using `uname -s`
2. **Checks for Python3** and installs if missing using your system's package manager
3. **Checks for pip** and installs if missing
4. **Checks for PyYAML** and installs via pip with appropriate flags for your platform

### Usage

```bash
make check-deps
```

This command is:
- **Idempotent**: Safe to run multiple times
- **Automatic**: Runs before `make run` and `make validate`
- **Smart**: Uses the right package manager for your platform

### Platform-Specific Behavior

#### macOS Special Handling
Modern macOS with Homebrew-managed Python uses PEP 668 "externally-managed environments". The Makefile handles this by:
1. First trying `pip install --user` (user-local install)
2. Falling back to `pip install --break-system-packages` if needed
3. As a last resort, installing libyaml via Homebrew

#### Linux Package Manager Fallback
On Linux, the Makefile tries package managers in this order:
1. `apt-get` (Debian/Ubuntu)
2. `yum` (RHEL/CentOS)
3. `dnf` (Fedora/RHEL 8+)
4. Falls back to `python3 -m ensurepip` if no package manager found

## Manual Installation

If you prefer to install dependencies manually:

### FreeBSD
```bash
sudo pkg install python3 py311-pip
python3 -m pip install --user pyyaml
```

### macOS
```bash
brew install python3
python3 -m pip install --user pyyaml
# Or if that fails:
python3 -m pip install --break-system-packages pyyaml
```

### Linux (Debian/Ubuntu)
```bash
sudo apt-get update
sudo apt-get install python3 python3-pip
python3 -m pip install --user pyyaml
```

### Linux (RHEL/CentOS/Fedora)
```bash
sudo dnf install python3 python3-pip
python3 -m pip install --user pyyaml
```

## Verifying Installation

After running `make check-deps`, verify everything works:

```bash
# Check Python version
python3 --version

# Check pip
python3 -m pip --version

# Check PyYAML
python3 -c "import yaml; print('PyYAML version:', yaml.__version__)"

# Test Ollama connection
make validate
```

## Troubleshooting

### macOS: "externally-managed-environment" error
This is expected with Homebrew-managed Python. The Makefile automatically handles this by using `--break-system-packages` or `--user` flags.

### Linux: No package manager found
If you're using an unsupported Linux distribution, manually install Python3 and pip, then run:
```bash
python3 -m pip install --user pyyaml
```

### FreeBSD: pkg not found
Make sure pkg is bootstrapped:
```bash
sudo pkg bootstrap
```

### All Platforms: pip not found
Try installing pip manually:
```bash
# Using ensurepip (cross-platform)
python3 -m ensurepip --upgrade

# Or download get-pip.py
curl https://bootstrap.pypa.io/get-pip.py | python3
```

## Build System Cross-Platform Support

The AI Code Reviewer can validate code using **any build system** on any platform:

| Build System | FreeBSD | macOS | Linux | Example Command |
|--------------|---------|-------|-------|-----------------|
| GNU Make | ✓ | ✓ | ✓ | `make -j$(nproc)` |
| BSD Make | ✓ | ✓ | ✓ | `make -j$(sysctl -n hw.ncpu)` |
| CMake | ✓ | ✓ | ✓ | `cmake --build build -j` |
| Cargo (Rust) | ✓ | ✓ | ✓ | `cargo build --release` |
| Go | ✓ | ✓ | ✓ | `go build ./...` |
| Python | ✓ | ✓ | ✓ | `python -m pytest` |
| Node.js | ✓ | ✓ | ✓ | `npm test` |

Just configure your build command in `config.yaml`!

## Contributing

When adding new platform support:

1. Add OS detection case in `Makefile` `check-deps` target
2. Use appropriate package manager for that platform
3. Test on actual hardware/VM
4. Update this document
5. Add example in README.md

## Testing

To test platform detection without installing packages:

```bash
# Check detected OS
uname -s

# Dry-run (won't actually install)
make -n check-deps
```

