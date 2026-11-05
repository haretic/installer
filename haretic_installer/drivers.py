"""What a computer needs before it can talk to the r1 over USB.

Windows: mtkclient reaches the r1 through libusb's UsbDk backend, so UsbDk must be installed. It
is a filter driver, so it takes every r1 USB function (boot ROM 0E8D:0003, preloader 0E8D:2000,
download agent 0E8D:2001) without replacing the COM port driver Windows binds to the preloader.
Linux: a udev rule gives the logged-in user those three functions and keeps ModemManager off the
preloader's serial port. macOS needs nothing."""
import hashlib
import os
import subprocess
import sys
import time
import urllib.request

from . import r1

USBDK = {'url': 'https://github.com/daynix/UsbDk/releases/download/v1.00-22/UsbDk_1.0.22_x64.msi',
         'name': 'UsbDk_1.0.22_x64.msi', 'size': 6348800,
         'sha256': '91f6f695e1e13c656024e6d3b55620bf08d8835ef05ee0496935ba6bb62466a5'}
RULE_NAME = '51-haretic-r1.rules'
# /run/host: the host's own folders, seen from a Flatpak sandbox
RULE_DIRS = ('/etc/udev/rules.d', '/usr/lib/udev/rules.d', '/lib/udev/rules.d',
             '/run/host/etc/udev/rules.d', '/run/host/usr/lib/udev/rules.d')
RULES = ''.join(
    f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{r1.VENDOR:04x}", ATTR{{idProduct}}=="{pid:04x}", '
    f'MODE="0666", TAG+="uaccess", ENV{{ID_MM_DEVICE_IGNORE}}="1"  # rabbit r1 {name}\n'
    for pid, name in r1.USB_IDS.items()) + \
    f'SUBSYSTEMS=="usb", ATTRS{{idVendor}}=="{r1.VENDOR:04x}", ENV{{ID_MM_DEVICE_IGNORE}}="1"\n'


def needed():
    """None if USB is ready, else 'usbdk' or 'udev'."""
    if sys.platform == 'win32':
        return None if usbdk_installed() else 'usbdk'
    if sys.platform.startswith('linux'):
        if os.geteuid() == 0 or any(os.path.exists(os.path.join(d, RULE_NAME)) for d in RULE_DIRS):
            return None
        return 'udev'
    return None


def usbdk_installed():
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Services\UsbDk'))
        return True
    except OSError:
        return False


def download_usbdk(folder, progress=None):
    """The UsbDk installer, checked against its pinned SHA-256."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, USBDK['name'])
    if os.path.exists(path) and _sha(path) == USBDK['sha256']:
        return path
    req = urllib.request.Request(USBDK['url'], headers={'User-Agent': 'haretic-installer'})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(path + '.part', 'wb') as out:
        done = 0
        while chunk := r.read(1 << 16):
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, USBDK['size'])
    if h.hexdigest() != USBDK['sha256']:
        os.remove(path + '.part')
        raise RuntimeError('The downloaded USB driver is not the one expected (checksum differs). '
                           'Try again.')
    os.replace(path + '.part', path)
    return path


def install_usbdk(msi):
    """Run the UsbDk installer as administrator (Windows asks for permission) and wait."""
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
                    ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                    ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    info = Info()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = 'runas'
    info.lpFile = 'msiexec.exe'
    info.lpParameters = f'/i "{msi}" /qb /norestart'
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise RuntimeError('Windows did not start the driver installer (permission refused?).')
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 600000)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    if code.value not in (0, 3010) or not usbdk_installed():
        raise RuntimeError(f'The driver installer did not finish (code {code.value}).')
    return code.value == 3010  # Windows wants a restart


def install_udev():
    """Install the udev rule with pkexec (asks for the password); outside a Flatpak sandbox
    through flatpak-spawn."""
    script = (f'printf %s "$1" > /etc/udev/rules.d/{RULE_NAME} && udevadm control --reload-rules '
              '&& udevadm trigger --subsystem-match=usb')
    cmd = ['pkexec', 'sh', '-c', script, 'sh', RULES]
    if os.path.exists('/.flatpak-info'):
        cmd = ['flatpak-spawn', '--host'] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RuntimeError('USB access was not set up' + (f': {r.stderr.strip()}' if r.stderr.strip() else
                                                          ' (the password prompt was closed).'))
    time.sleep(1)


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
