#!/usr/bin/env bash
# Prepare an Ubuntu/Debian VPS to run the browser-based Quotex connection test.
# Usage: bash vps/setup_ubuntu.sh   (from the repo root)
#
# Isolation: nothing system-wide is replaced or reconfigured.
#  - Python packages go into ./.venv (no global pip)
#  - Chrome + chromedriver are downloaded into ./vps/browser (your system Chrome is untouched)
#  - apt only installs packages that are MISSING (it never upgrades or removes anything)
#  - no services, no fixed ports, no fixed X display (xvfb-run -a picks a free one)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# --- 1. missing OS packages (Chrome runtime libs + virtual display) -------------------
PKGS="python3 python3-venv xvfb unzip curl ca-certificates libnss3 libgbm1 libasound2 \
libatk-bridge2.0-0 libgtk-3-0 libxss1 libxkbcommon0 fonts-liberation"
MISSING=""
for p in $PKGS; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
        MISSING="$MISSING $p"
    fi
done
if [ -n "$MISSING" ]; then
    echo "Installing missing packages:$MISSING"
    $SUDO apt-get update
    $SUDO apt-get install -y --no-install-recommends $MISSING
else
    echo "All OS packages already present."
fi

# --- 2. private Chrome for Testing (matching chrome + chromedriver) -------------------
BROWSER_DIR="$ROOT/vps/browser"
if [ ! -x "$BROWSER_DIR/chrome-linux64/chrome" ]; then
    mkdir -p "$BROWSER_DIR"
    JSON="$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json)"
    python3 - "$JSON" "$BROWSER_DIR" <<'EOF'
import json, sys, urllib.request, zipfile, io, os
data = json.loads(sys.argv[1])["channels"]["Stable"]
dest = sys.argv[2]
print("Chrome for Testing version:", data["version"])
for kind in ("chrome", "chromedriver"):
    url = next(d["url"] for d in data["downloads"][kind] if d["platform"] == "linux64")
    print("Downloading", url)
    with urllib.request.urlopen(url) as r:
        zipfile.ZipFile(io.BytesIO(r.read())).extractall(dest)
EOF
    chmod +x "$BROWSER_DIR/chrome-linux64/chrome" "$BROWSER_DIR/chromedriver-linux64/chromedriver"
fi
"$BROWSER_DIR/chrome-linux64/chrome" --version || echo "WARNING: chrome failed to start, a system library is probably missing"

# --- 3. isolated Python environment ----------------------------------------------------
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt" -e "$ROOT"

echo
echo "Setup done. Test with:"
echo "  cd $ROOT"
echo "  xvfb-run -a .venv/bin/python vps/probe_browser.py"
