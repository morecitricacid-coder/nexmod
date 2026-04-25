#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

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

# ── Virtual environment ───────────────────────────────────────────────────────
echo "Creating venv..."
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# ── Wrapper install ───────────────────────────────────────────────────────────
# Try /usr/local/bin first (system-wide); fall back to ~/.local/bin (no sudo needed).
if [ -w /usr/local/bin ] || sudo -n true 2>/dev/null; then
    WRAPPER="/usr/local/bin/nexmod"
    sudo tee "$WRAPPER" > /dev/null <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python3" "$SCRIPT_DIR/nexmod.py" "\$@"
EOF
    sudo chmod +x "$WRAPPER"
    echo ""
    echo "nexmod installed → $WRAPPER  (system-wide)"
else
    WRAPPER="$HOME/.local/bin/nexmod"
    mkdir -p "$HOME/.local/bin"
    cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python3" "$SCRIPT_DIR/nexmod.py" "\$@"
EOF
    chmod +x "$WRAPPER"
    echo ""
    echo "nexmod installed → $WRAPPER  (user-local, no sudo required)"
    if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
        echo ""
        echo "Note: add ~/.local/bin to your PATH if 'nexmod' isn't found:"
        echo "  Bash:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
        echo "  Zsh:   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
        echo "  Fish:  fish_add_path ~/.local/bin"
    fi
fi

echo ""
echo "Next steps:"
echo "  1. Get your API key: nexusmods.com → avatar → Settings → API Keys"
echo "  2. nexmod config set-key <your-key>"
echo "  3. nexmod config verify          # confirm premium"
echo "  4. nexmod install darktide <mod_id>"
