#!/usr/bin/env bash
# nexmod installer — uses pip editable install so no path hardcoding.
# Requires Python 3.10+. Installs into ~/.local/ (no sudo needed).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Python version check ──────────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found. Install it with your package manager:" >&2
    echo "  Ubuntu/Debian: sudo apt install python3" >&2
    echo "  Arch:          sudo pacman -S python" >&2
    echo "  Fedora:        sudo dnf install python3" >&2
    exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Error: nexmod requires Python 3.10+, found $PY_VERSION" >&2
    echo "Check your distro's python3 package or use pyenv." >&2
    exit 1
fi

echo "Python $PY_VERSION — OK"

# ── Install ───────────────────────────────────────────────────────────────────
echo "Installing nexmod..."
"$PYTHON" -m pip install -q -e "$SCRIPT_DIR" --user

# ── PATH hint ─────────────────────────────────────────────────────────────────
if ! command -v nexmod &>/dev/null; then
    echo ""
    echo "Note: ~/.local/bin is not in your PATH. Add it:"
    echo "  Bash:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
    echo "  Zsh:   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    echo "  Fish:  fish_add_path ~/.local/bin"
    echo ""
    echo "Then run 'nexmod' to get started."
else
    echo ""
    echo "nexmod installed → $(command -v nexmod)"
fi

echo ""
echo "Next steps:"
echo "  1. Get your API key: nexusmods.com → avatar → Settings → API Keys"
echo "  2. nexmod config set-key <your-key>"
echo "  3. nexmod config verify          # confirm premium"
echo "  4. nexmod install darktide <mod_id>"
