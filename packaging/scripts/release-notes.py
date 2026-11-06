#!/usr/bin/env python3
"""The release notes: the tag's summary from packaging/release-notes/<tag>.md, then a table of
every download. release-notes.py <tag> <dist-folder>"""
import pathlib
import sys
from urllib.parse import quote

tag, dist = sys.argv[1], pathlib.Path(sys.argv[2])
root = pathlib.Path(__file__).resolve().parents[1]
summary = root / 'release-notes' / (tag + '.md')
if summary.is_file():
    print(summary.read_text().strip() + '\n')
base = 'https://github.com/haretic/installer/releases/download/' + quote(tag, safe='') + '/'
kinds = [('-setup.exe', 'Setup'), ('-portable.exe', 'Portable EXE'), ('.AppImage', 'AppImage'),
         ('.deb', '.deb'), ('.flatpak', 'Flatpak'), ('.zip', 'App ZIP')]
rows = []
for system, arch, label in [('windows', 'x64', 'Windows x64'), ('linux', 'x64', 'Linux x64'),
                            ('linux', 'arm64', 'Linux ARM64'), ('macos', 'x64', 'macOS Intel'),
                            ('macos', 'arm64', 'macOS Apple Silicon')]:
    links = []
    for suffix, kind in kinds:
        for p in sorted(dist.glob(f'*-{system}-{arch}*')):
            if p.name.endswith(suffix):
                links.append(f'[{kind}]({base}{quote(p.name, safe="")})')
    if links:
        rows.append(f'| {label} | ' + ' - '.join(links) + ' |')
print('| Platform | Download |\n| --- | --- |\n' + '\n'.join(rows))
print(f'\n[SHA256 checksums]({base}SHA256SUMS)')
print('\nThe portable Windows EXE and the AppImage keep their downloads, backups and logs in a '
      '`haretic-installer-data` folder next to themselves. On first start, Windows installs the '
      'UsbDk USB driver (asking for permission) and Linux asks for your password once to allow '
      'USB access to the r1. macOS needs nothing.')
