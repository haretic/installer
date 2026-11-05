"""Where the installer keeps downloads, backups and logs. Portable builds (the Windows -portable
exe, the AppImage) keep them in a folder next to themselves; installed builds in the user's data
folder."""
import os
import sys

try:
    from . import _build
except ImportError:  # running from source
    _build = None

VERSION = getattr(_build, 'VERSION', 'dev')
NAME = 'haretic installer'
FOLDER = 'haretic-installer-data'


def portable_root():
    """The folder a portable build sits in, or None for an installed build."""
    if os.environ.get('APPIMAGE'):
        return os.path.dirname(os.path.abspath(os.environ['APPIMAGE']))
    if getattr(_build, 'PORTABLE', False) and getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


def data_dir():
    root = portable_root()
    if root:
        return os.path.join(root, FOLDER)
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return os.path.join(base, 'haretic', 'installer')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/haretic installer')
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'haretic-installer')


def sub(name):
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def downloads():
    return sub('downloads')


def backups():
    return sub('backups')


def logs():
    return sub('logs')


def work():
    return sub('mtkclient')


def resource(*parts):
    """A file shipped with the app, frozen or from source."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)
