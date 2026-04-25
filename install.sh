#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

echo "Creating venv..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# Write a launcher wrapper so you don't need to activate the venv
WRAPPER="/usr/local/bin/nexmod"
sudo tee "$WRAPPER" > /dev/null <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python3" "$SCRIPT_DIR/nexmod.py" "\$@"
EOF
sudo chmod +x "$WRAPPER"

echo ""
echo "nexmod installed → $WRAPPER"
echo ""
echo "Next steps:"
echo "  1. Get your API key: nexusmods.com → avatar → Settings → API Keys"
echo "  2. nexmod config set-key <your-key>"
echo "  3. nexmod config verify          # confirm premium"
echo "  4. nexmod install darktide <mod_id>"
