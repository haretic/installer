#!/usr/bin/env bash
# The Linux folder build as a .deb and an AppImage (data next to the AppImage). Run from the
# repository root after pyinstaller; VERSION and ARCH (x64|arm64) set.
set -euo pipefail
version=${VERSION#installer-v}
version=${version#v}
deb_version=${version/-/\~}
case "$ARCH" in
	x64) debarch=amd64; apparch=x86_64; toolsum=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0;;
	arm64) debarch=arm64; apparch=aarch64; toolsum=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158;;
	*) exit 1;;
esac
bundle=build/dist/haretic-installer
# The bundle needs at least the glibc it was built against
glibc=$(ldd --version | sed -n '1s/.* //p')
mkdir -p dist

pkg=build/deb
rm -rf "$pkg"
mkdir -p "$pkg/DEBIAN" "$pkg/usr/lib" "$pkg/usr/bin" "$pkg/usr/share/applications" \
	"$pkg/usr/share/icons/hicolor/scalable/apps" "$pkg/usr/share/metainfo" "$pkg/usr/lib/udev/rules.d" \
	"$pkg/usr/share/doc/haretic-installer"
cp -a "$bundle" "$pkg/usr/lib/haretic-installer"
ln -s ../lib/haretic-installer/haretic-installer "$pkg/usr/bin/haretic-installer"
cp packaging/io.github.haretic.installer.desktop "$pkg/usr/share/applications/"
cp packaging/io.github.haretic.installer.svg "$pkg/usr/share/icons/hicolor/scalable/apps/"
cp packaging/io.github.haretic.installer.metainfo.xml "$pkg/usr/share/metainfo/"
cp LICENSE "$pkg/usr/share/doc/haretic-installer/copyright"
# The r1's USB functions for the logged-in user, ModemManager kept off them
python3 -c 'import sys; sys.path.insert(0, "."); from haretic_installer import drivers; print(drivers.RULES, end="")' \
	> "$pkg/usr/lib/udev/rules.d/51-haretic-r1.rules"
cat > "$pkg/DEBIAN/control" <<CONTROL
Package: haretic-installer
Version: $deb_version
Architecture: $debarch
Maintainer: haretic <0@kenny.cat>
Depends: libc6 (>= $glibc), libfontconfig1, libfreetype6, libxkbcommon0, libxkbcommon-x11-0, libgl1, libegl1, libdbus-1-3, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xinerama0, libxcb-randr0, libxcb-render-util0, libxcb-image0, libwayland-client0, libwayland-cursor0
Section: utils
Priority: optional
Homepage: https://github.com/haretic/mtkclient
Description: Install haretic on a rabbit r1
 A guided installer for haretic, Android (LineageOS) for the rabbit r1: checks the
 device, backs it up, unlocks its bootloader with mtkclient, installs and starts haretic.
CONTROL
cat > "$pkg/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
	udevadm control --reload-rules || true
	udevadm trigger --subsystem-match=usb || true
fi
POST
chmod 755 "$pkg/DEBIAN/postinst"
dpkg-deb --root-owner-group -Zxz --build "$pkg" "dist/haretic-installer-${version}-linux-${ARCH}.deb"

appdir=build/AppDir
rm -rf "$appdir"
mkdir -p "$appdir/usr/lib" "$appdir/usr/share/metainfo"
cp -a "$bundle" "$appdir/usr/lib/haretic-installer"
cp packaging/io.github.haretic.installer.desktop packaging/io.github.haretic.installer.svg "$appdir/"
cp packaging/io.github.haretic.installer.png "$appdir/.DirIcon"
cp packaging/io.github.haretic.installer.metainfo.xml "$appdir/usr/share/metainfo/"
cat > "$appdir/AppRun" <<'RUN'
#!/bin/sh
here=$(dirname "$(readlink -f "$0")")
exec "$here/usr/lib/haretic-installer/haretic-installer" "$@"
RUN
chmod 755 "$appdir/AppRun"
curl --fail --location --retry 3 -o build/appimagetool \
	"https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-${apparch}.AppImage"
printf '%s  %s\n' "$toolsum" build/appimagetool | sha256sum --check
chmod +x build/appimagetool
ARCH=$apparch APPIMAGE_EXTRACT_AND_RUN=1 build/appimagetool --comp zstd "$appdir" \
	"dist/haretic-installer-${version}-linux-${ARCH}.AppImage"
