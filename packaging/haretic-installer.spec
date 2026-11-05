# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: the folder build (setup, deb, Flatpak, AppImage, macOS app) or, with
# HI_ONEFILE=1, the single-file Windows -portable exe. Run from the repository root.
import os
import subprocess
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPECPATH))
R = lambda *p: os.path.join(ROOT, *p)  # noqa: E731
onefile = os.environ.get('HI_ONEFILE') == '1'
binaries = []
if sys.platform.startswith('linux'):
    # libusb's runtime library, which mtkclient loads from the bundle first
    lib = subprocess.run(['sh', '-c', 'PATH=$PATH:/sbin:/usr/sbin ldconfig -p | sed -n "s/.*libusb-1.0.so.0 (.*) => //p" | head -1'],
                         capture_output=True, text=True).stdout.strip()
    if not lib:
        import glob
        lib = next(iter(sorted(glob.glob('/usr/lib/*-linux-gnu/libusb-1.0.so.0') + glob.glob('/lib/*-linux-gnu/libusb-1.0.so.0'))), '')
    if not lib:
        raise SystemExit('libusb-1.0.so.0 is not installed (apt install libusb-1.0-0)')
    binaries.append((lib, '.'))  # under its soname, which mtkclient loads
elif sys.platform == 'darwin':
    prefix = subprocess.run(['brew', '--prefix', 'libusb'], capture_output=True, text=True).stdout.strip()
    if not prefix:
        raise SystemExit('libusb is not installed (brew install libusb)')
    binaries.append((os.path.realpath(os.path.join(prefix, 'lib', 'libusb-1.0.dylib')), '.'))
datas = [(R('mtkclient/Loader/MTK_DA_V5.bin'), 'mtkclient/Loader'),
         (R('mtkclient/Loader/Preloader'), 'mtkclient/Loader/Preloader'),
         (R('mtkclient/payloads'), 'mtkclient/payloads'), (R('packaging/io.github.haretic.installer.png'), '.'),
         (R('LICENSE'), '.')]
if sys.platform == 'win32':
    datas.append((R('mtkclient/Windows'), 'mtkclient/Windows'))
hidden = collect_submodules('mtkclient.Library') + collect_submodules('mtkclient.config') + \
    collect_submodules('haretic_installer')

a = Analysis([R('packaging/launcher.py')], pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=['tkinter', 'mtkclient.gui', 'capstone', 'keystone', 'unicorn', 'PySide6.QtWebEngineCore', 'PySide6.QtQuick', 'PySide6.QtQml',
                       'PySide6.Qt3DCore', 'PySide6.QtMultimedia', 'PySide6.QtPdf', 'PySide6.QtCharts'],
             noarchive=False)
pyz = PYZ(a.pure)
icon = {'win32': R('packaging/haretic-installer.ico'), 'darwin': R('packaging/haretic-installer.icns')}.get(sys.platform)
console = os.environ.get('HI_CONSOLE') == '1'
if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='haretic-installer', icon=icon, console=console,
              upx=False, runtime_tmpdir=None)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='haretic-installer', icon=icon, console=console,
              upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name='haretic-installer', upx=False)
    if sys.platform == 'darwin':
        app = BUNDLE(coll, name='haretic installer.app', icon=icon, bundle_identifier='io.github.haretic.installer',
                     info_plist={'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '12.0',
                                 'CFBundleShortVersionString': os.environ.get('HI_VERSION', '0.0.0')})
