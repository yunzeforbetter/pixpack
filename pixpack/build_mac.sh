#!/bin/sh
# Build dist/PixPack.app. Run this on a Mac; PyInstaller cannot cross-compile.
set -eu
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Install Python 3.10 or newer."
    exit 1
fi

# Quit a running copy so the bundle can be replaced.
osascript -e 'tell application "PixPack" to quit' >/dev/null 2>&1 || true

python3 -m pip install -r requirements.txt -r requirements-build.txt
python3 -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name PixPack \
    --osx-bundle-identifier dev.pixpack.app \
    --collect-submodules PIL \
    pixpack_gui.py

echo
echo "Built dist/PixPack.app"
