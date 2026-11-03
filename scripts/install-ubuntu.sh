#!/usr/bin/env bash
# Installs NINA from this checkout on Ubuntu/Debian: system libraries, a virtualenv and an application-menu entry.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTRAS="${1:-}"        # pass "agents" to also install the optional managed-agents support (~300 MB)

echo "==> Installing system libraries (sudo will ask for your password)"
sudo apt-get update -qq
sudo apt-get install -y python3-venv libxcb-cursor0 libxkbcommon-x11-0

echo "==> Creating the virtual environment and installing NINA"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip -q
if [ -n "$EXTRAS" ]; then
  "$DIR/.venv/bin/pip" install -q -e "$DIR[$EXTRAS]"
else
  "$DIR/.venv/bin/pip" install -q -e "$DIR"
fi

echo "==> Creating the application-menu entry"
mkdir -p "$HOME/.local/share/applications"
ICON="$("$DIR/.venv/bin/python" -c 'from nina.config import ICON_FILE; print(ICON_FILE)')"
cat > "$HOME/.local/share/applications/nina.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=NINA
Comment=Pixel-art mascot for your AI coding agents
Exec="$DIR/.venv/bin/nina"
Icon=$ICON
Terminal=false
Categories=Utility;Development;
DESKTOP

echo
echo "Done. Start NINA from the application menu, or run:  $DIR/.venv/bin/nina"
echo "Then right click the mascot and enable 'Connect to Claude Code' (or run: nina --install-hooks)."
