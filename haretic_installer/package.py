"""haretic install packages: an xz-compressed tar of the images an r1 needs, its manifest of their
hashes first. Read from a file or downloaded from haretic's GitHub releases, then unpacked once
(and checked on the way) next to the download."""
import contextlib
import hashlib
import io
import json
import re
import lzma
import os
import shutil
import subprocess
import tarfile
import urllib.request

from . import r1, sparse

RELEASES = 'https://api.github.com/repos/haretic/release/releases'
# Named as GSIs are: haretic-<date>-<time>-<channel>[-aosp]-r1_b<v|g><N|S>.tar.xz, b an A/B device, v
# vanilla or g with Google apps, N not rooted or S rooted (Magisk). -aosp: plain LineageOS with only
# what the r1 needs to run, none of haretic's changes; offered, not recommended. Packages before the
# time was added have none.
NAME = re.compile(r'^(?P<version>haretic-\d{8}(?:-\d{4})?-[a-z]+)(?P<aosp>-aosp)?-r1_b(?P<apps>[vg])(?P<root>[NS])\.tar\.xz$')
SUFFIX = '.tar.xz'


def variant(gapps, root):
    return 'r1_b' + ('g' if gapps else 'v') + ('S' if root else 'N')


def file_name(version, gapps, root, aosp=False):
    return f'{version}{"-aosp" if aosp else ""}-{variant(gapps, root)}{SUFFIX}'


def describe(gapps, root, aosp=False):
    return (('plain LineageOS (aosp), ' if aosp else '') + ('with Google apps' if gapps else 'without Google apps')
            + (', rooted (Magisk)' if root else ''))
# What a package holds, in its order: (key, file, the most its partition takes). super is a
# sparse image and comes last, the biggest.
IMAGES = (('lk', 'lk.bin', r1.LK_PAYLOAD), ('logo', 'logo.bin', 0xb00000), ('dtbo', 'dtbo.img', 0x800000),
          ('vbmeta', 'vbmeta.img', 0x800000), ('boot', 'boot.img', 0x2000000), ('super', 'super.img', None))
CHUNK = 4 << 20


class PackageError(Exception):
    pass


def _manifest(path):
    """The manifest, the archive's first member, without reading the rest."""
    try:
        with tarfile.open(path, 'r:xz') as t:
            first = t.next()
            if first is None or first.name != 'manifest.json' or not first.isfile():
                raise PackageError('This is not a haretic install package (no manifest first).')
            return json.load(t.extractfile(first))
    except (OSError, tarfile.TarError, EOFError, ValueError) as e:
        raise PackageError(f'This is not a haretic install package ({e}).')


class Package:
    def __init__(self, path, unpack_root=None):
        self.path = path
        self.manifest = m = _manifest(path)
        if not isinstance(m, dict) or m.get('schema') != 1 or m.get('product') != 'haretic_r1':
            raise PackageError('This package is not for the rabbit r1, or needs a newer installer.')
        for key, name, limit in IMAGES:
            image = m.get('images', {}).get(key)
            if not image or image.get('file') != name or not isinstance(image.get('size'), int) \
                    or not isinstance(image.get('sha256'), str):
                raise PackageError(f'The package has no {name}.')
            if limit and image['size'] > limit:
                raise PackageError(f'{name} is larger than its partition.')
        # An r1 is told apart from rabbitOS by its LK: one this installer doesn't know would leave
        # the r1 looking like rabbitOS after the install, refused updates and its backups misnamed
        if m['images']['lk']['size'] != r1.LK_PAYLOAD or m['images']['lk']['sha256'] not in r1.LK_HARETIC:
            raise PackageError('This package has a haretic bootloader this installer does not know yet. '
                               'Get the newest haretic installer.')
        if not isinstance(m.get('version'), str):
            raise PackageError('The package does not say which haretic it is.')
        self.version = m['version']
        self.gapps = bool(m.get('gapps'))
        self.root = bool(m.get('root'))
        self.aosp = bool(m.get('aosp'))
        root = unpack_root or os.path.dirname(os.path.abspath(path))
        self.folder = os.path.join(root, os.path.basename(path)[:-len(SUFFIX)])
        self.stamp = os.path.join(self.folder, 'checked.json')

    def size(self, key):
        return self.manifest['images'][key]['size']

    def _file(self, key):
        return os.path.join(self.folder, self.manifest['images'][key]['file'])

    def open(self, key):
        return open(self._file(key), 'rb')

    def read(self, key):
        with self.open(key) as f:
            return f.read()

    def _mark(self):
        st = os.stat(self.path)
        return {'archive_size': st.st_size, 'archive_mtime': int(st.st_mtime),
                'manifest': hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()}

    def checked(self):
        try:
            with open(self.stamp) as f:
                ok = json.load(f) == self._mark()
        except (OSError, ValueError):
            return False
        return ok and all(os.path.getsize(self._file(k)) == self.size(k) for k, _, _ in IMAGES)

    def verify(self, progress=None, cancelled=lambda: False):
        """Unpack every image beside the archive, checking it against the manifest's SHA-256;
        progress(done, total) in bytes. Done once: a stamp remembers a package that passed."""
        if self.checked():
            return True
        os.makedirs(self.folder, exist_ok=True)
        if os.path.exists(self.stamp):
            os.remove(self.stamp)
        total = sum(self.size(k) for k, _, _ in IMAGES)
        names = {name: key for key, name, _ in IMAGES}
        done = 0
        seen = set()
        try:
            with tarfile.open(self.path, 'r|xz') as t:
                for member in t:
                    if member.name == 'manifest.json':
                        continue
                    key = names.get(member.name)
                    if key is None or not member.isfile() or key in seen:
                        raise PackageError(f'The package holds an unexpected file: {member.name}.')
                    seen.add(key)
                    h = hashlib.sha256()
                    n = 0
                    src = t.extractfile(member)
                    with open(self._file(key) + '.part', 'wb') as out:
                        while chunk := src.read(CHUNK):
                            if cancelled():
                                return False
                            h.update(chunk)
                            out.write(chunk)
                            n += len(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                    image = self.manifest['images'][key]
                    if n != image['size'] or h.hexdigest() != image['sha256']:
                        raise PackageError(f'{member.name} in the package is damaged (its checksum '
                                           'differs). Download it again.')
                    os.replace(self._file(key) + '.part', self._file(key))
        except (OSError, tarfile.TarError, EOFError) as e:
            raise PackageError(f'The package could not be read to the end ({e}). Download it again.')
        if seen != set(names.values()):
            raise PackageError('The package is missing images.')
        with self.open('super') as f:
            _block, total_bytes, _, _ = sparse.header(f)
        if total_bytes != dict((n, s) for n, _, s in r1.LAYOUT)['super']:
            raise PackageError('super.img does not fit the r1 super partition.')
        if self.read('boot')[:8] != b'ANDROID!':
            raise PackageError('boot.img is not an Android boot image.')
        with open(self.stamp, 'w') as f:
            json.dump(self._mark(), f)
        return True


def _get(url, accept='application/vnd.github+json'):
    req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'haretic-installer'})
    return urllib.request.urlopen(req, timeout=30)


def latest():
    """The install packages of the newest release that has any: [{version, name, url, size,
    sha256, notes, prerelease, gapps, root}], vanilla unrooted first; [] if none is published."""
    with _get(RELEASES + '?per_page=20') as r:
        releases = json.load(r)
    for rel in releases:
        if rel.get('draft'):
            continue
        found = []
        for asset in rel.get('assets', []):
            name = NAME.match(asset['name'])
            if name:
                digest = asset.get('digest') or ''
                found.append({'version': name['version'], 'root': name['root'] == 'S', 'aosp': bool(name['aosp']),
                              'name': asset['name'], 'url': asset['browser_download_url'],
                              'size': asset['size'], 'prerelease': rel.get('prerelease', False),
                              'sha256': digest[7:] if digest.startswith('sha256:') else None,
                              'notes': rel.get('body') or '', 'gapps': name['apps'] == 'g'})
        if found:
            return sorted(found, key=lambda a: (a['aosp'], a['gapps'], a['root']))
    return []


def download(release, folder, progress=None, cancelled=lambda: False):
    """Download a release's package into folder, resuming a partial download; returns its path.
    Checked against GitHub's SHA-256 of the file when GitHub gives one."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, release['name'])
    part = path + '.part'
    if os.path.exists(path) and os.path.getsize(path) == release['size']:
        if not release['sha256'] or _file_sha(path) == release['sha256']:
            return path
        os.remove(path)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > release['size']:
        os.remove(part)
        have = 0
    req = urllib.request.Request(release['url'], headers={'User-Agent': 'haretic-installer'})
    if have:
        req.add_header('Range', f'bytes={have}-')
    # A whole .part, stopped before its check: asking for the bytes after its end gets a 416
    with contextlib.nullcontext() if have == release['size'] else urllib.request.urlopen(req, timeout=60) as r:
        if r and have and r.status != 206:
            have = 0  # the server ignored the range: start again
        with open(part, 'ab' if have else 'wb') as out:
            done = have
            while r and (chunk := r.read(1 << 20)):
                if cancelled():
                    return None
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, release['size'])
    if os.path.getsize(part) != release['size']:
        raise PackageError('The download ended early. Try again; it continues where it stopped.')
    if release['sha256'] and _file_sha(part) != release['sha256']:
        os.remove(part)
        raise PackageError('The download is damaged (its checksum differs). Try again.')
    os.replace(part, path)
    return path


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def build(out, images, version, gapps=False, root=False, aosp=False, threads=0):
    """Write an install package: images maps each IMAGES key to a file path. Compressed as much as
    xz goes (-9e), with the xz tool on all cores when there is one, else Python's lzma."""
    manifest = {'schema': 1, 'product': 'haretic_r1', 'version': version, 'gapps': gapps, 'root': root, 'aosp': aosp,
                'images': {}}
    for key, name, _ in IMAGES:
        manifest['images'][key] = {'file': name, 'size': os.path.getsize(images[key]),
                                   'sha256': _file_sha(images[key])}
    blob = (json.dumps(manifest, indent=1) + '\n').encode()
    xz = shutil.which('xz')
    with open(out + '.part', 'wb') as dest:
        proc = subprocess.Popen([xz, f'-T{threads}', '-9e', '-c'], stdin=subprocess.PIPE, stdout=dest) if xz else None
        with tarfile.open(fileobj=proc.stdin, mode='w|') if proc else tarfile.open(fileobj=dest, mode='w|xz', preset=9 | lzma.PRESET_EXTREME) as t:
            info = tarfile.TarInfo('manifest.json')
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))
            for key, name, _ in IMAGES:
                info = tarfile.TarInfo(name)
                info.size = os.path.getsize(images[key])
                with open(images[key], 'rb') as f:
                    t.addfile(info, f)
        if proc:
            proc.stdin.close()
            if proc.wait():
                raise PackageError('xz failed')
    os.replace(out + '.part', out)
    return manifest
