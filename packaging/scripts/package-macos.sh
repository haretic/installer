#!/usr/bin/env bash
# The macOS app, ad-hoc signed, zipped. Run from the repository root after pyinstaller;
# VERSION and ARCH (x64|arm64) set.
set -euo pipefail
version=${VERSION#installer-v}
version=${version#v}
app='build/dist/haretic installer.app'
mkdir -p dist
codesign --force --deep --sign - "$app"
ditto -c -k --sequesterRsrc --keepParent "$app" "dist/haretic-installer-${version}-macos-${ARCH}.zip"
