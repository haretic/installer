"""The installer's steps, without a UI: check the r1, back it up, unlock it, install haretic, boot.
Each step reports through a Reporter and can be stopped between pieces; every write is read back.
"""
import datetime
import gzip
import hashlib
import json
import os
import shutil
import time

from . import r1, sparse
from .device import Cancelled, PIECE

PLACE = {n: (o, s) for n, o, s in r1.LAYOUT}
GPT_BYTES = 34 * r1.SECTOR
BACKUP_FREE = 12 << 30  # rabbitOS backs up to about 4-6 GB; leave room


class StepError(Exception):
    """A step failed. safe says whether the r1 is as it was before the step: nothing written yet,
    or only what a re-run writes again."""

    def __init__(self, message, safe=True, hint=None):
        super().__init__(message)
        self.safe = safe
        self.hint = hint


class Reporter:
    """What a step tells the UI. The UI replaces these."""

    def status(self, text):
        pass

    def progress(self, done, total):
        pass

    def log(self, line):
        pass

    def cancelled(self):
        return False


class Rate:
    """Bytes per second over the last half minute, for time-left estimates."""

    def __init__(self):
        self.marks = []

    def add(self, done):
        now = time.monotonic()
        self.marks.append((now, done))
        while len(self.marks) > 2 and now - self.marks[0][0] > 30:
            self.marks.pop(0)

    def left(self, done, total):
        if len(self.marks) < 2 or self.marks[-1][0] - self.marks[0][0] < 3:
            return None
        (t0, d0), (t1, d1) = self.marks[0], self.marks[-1]
        speed = (d1 - d0) / (t1 - t0)
        return (total - done) / speed if speed > 0 else None


class Info:
    """What the check found on the r1."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def system(self):
        return {'haretic': 'haretic', 'stock': 'rabbitOS'}.get(self.lk[self.slot], 'rabbitOS')

    def summary(self):
        return {'cid': self.identity['cid'], 'storage_gb': round(self.identity['user_size'] / 1e9),
                'slot': self.slot, 'lock': self.lock, 'lk_a': self.lk['a'], 'lk_b': self.lk['b'],
                'system': self.system}


class Session:
    def __init__(self, device, reporter):
        self.dev = device
        self.rep = reporter
        self.info = None
        self.done = 0
        self.total = 0
        self.rate = Rate()

    # -- progress over a whole step
    def begin(self, total, text=None):
        self.done, self.total, self.rate = 0, max(total, 1), Rate()
        if text:
            self.rep.status(text)
        self.rep.progress(0, self.total)

    def advance(self, n):
        self.done += n
        self.rate.add(self.done)
        self.rep.progress(min(self.done, self.total), self.total)

    def time_left(self):
        return self.rate.left(self.done, self.total)

    def check(self):
        if self.rep.cancelled():
            raise Cancelled()

    def read(self, area, offset, length, piece=PIECE):
        """Yield the bytes of a range in pieces, counting them."""
        at = 0
        while at < length:
            self.check()
            n = min(piece, length - at)
            data = self.dev.read(area, offset + at, n)
            self.advance(n)
            yield data
            at += n

    def write(self, offset, data, label):
        """Write bytes and read them back; a mismatch is an error, not a retry."""
        at = 0
        while at < len(data):
            self.check()
            piece = data[at:at + PIECE]
            self.dev.write(offset + at, piece)
            if self.dev.read('user', offset + at, len(piece)) != piece:
                raise StepError(f'The r1 did not store {label} correctly (read-back differs).',
                                safe=False, hint='Try a different USB cable or port, then run the '
                                'step again; it rewrites everything.')
            self.advance(2 * len(piece))
            at += len(piece)

    # -- 1. check
    def connect(self, status):
        self.rep.log('waiting for the r1 over USB')
        identity = self.dev.connect(status, self.rep.cancelled)
        self.rep.log('connected: ' + json.dumps(identity))
        return identity

    def inspect(self, identity):
        self.rep.status('Reading the partition table')
        raw = self.dev.read('user', 0, GPT_BYTES)
        parts = r1.parse_gpt(raw)
        r1.check_layout(parts)
        if identity['boot1_size'] != r1.BOOT_AREA or identity['boot2_size'] != r1.BOOT_AREA:
            raise r1.NotAnR1('Its boot areas are not the size an r1 has.')
        end = max(o + s for o, s in parts.values())
        if end > identity['user_size']:
            raise r1.NotAnR1('Its partitions run past the end of its storage.')
        self.rep.status('Reading the boot record, lock state and loaders')
        para, _ = PLACE['para']
        rec = self.dev.read('user', para + r1.RECORD_OFFSET, 32)
        if not r1.record_valid(rec):
            raise StepError('The r1 boot record is damaged, so the installer cannot tell which '
                            'system it runs. Nothing was changed.', hint='Start rabbitOS once, '
                            'then try again.')
        seccfg = self.dev.read('user', PLACE['seccfg'][0], 512)
        lock = r1.lock_state(seccfg)
        lk = {s: r1.lk_kind(self.dev.read('user', PLACE['lk_' + s][0], r1.LK_PAYLOAD)) for s in 'ab'}
        hwtype = None
        if lock != 'unlocked':
            self.rep.status('Checking that the r1 can be unlocked')
            hwtype = self.dev.seccfg_check(seccfg)
            if hwtype is None:
                raise StepError('This r1 has a lock record the installer cannot unlock. Nothing '
                                'was changed.')
        self.info = Info(identity=identity, parts=parts, gpt=raw, record=rec, slot=r1.record_slot(rec),
                         lock=lock, lk=lk, seccfg_type=hwtype)
        self.rep.log('found: ' + json.dumps(self.info.summary()))
        return self.info

    # -- 2. backup
    def backup_plan(self, include_userdata):
        plan = [('gpt', 'user', 0, GPT_BYTES), ('boot1', 'boot1', 0, r1.BOOT_AREA),
                ('boot2', 'boot2', 0, r1.BOOT_AREA)]
        for name, (offset, size) in sorted(self.info.parts.items(), key=lambda p: p[1][0]):
            if name != 'userdata' or include_userdata:
                plan.append((name, 'user', offset, size))
        size = self.info.identity['user_size']
        plan.append(('gpt_backup', 'user', size - 33 * r1.SECTOR, 33 * r1.SECTOR))
        return plan

    def backup(self, root, include_userdata=False):
        """Back up the r1 into a new or unfinished folder under root; returns the folder."""
        cid = self.info.identity['cid']
        folder = unfinished_backup(root, cid) or os.path.join(
            root, f'r1-backup-{cid[:8]}-{datetime.datetime.now():%Y%m%d-%H%M}')
        os.makedirs(folder, exist_ok=True)
        plan = self.backup_plan(include_userdata)
        need = sum(p[3] for p in plan) // 2
        free = shutil.disk_usage(folder).free
        if free < min(need, BACKUP_FREE):
            raise StepError(f'The backup needs about {fmt_size(min(need, BACKUP_FREE))} of space, '
                            f'but {folder} has {fmt_size(free)} free.',
                            hint='Choose another folder or free up space.')
        path = os.path.join(folder, 'manifest.json')
        manifest = json.load(open(path)) if os.path.exists(path) else {
            'schema': 1, 'kind': 'r1-backup', 'created': datetime.datetime.now().isoformat(timespec='seconds'),
            'device': self.info.summary(), 'parts': {}, 'complete': False}
        if manifest['device']['cid'] != cid:
            raise StepError('That folder holds another r1 backup.')
        manifest['userdata'] = include_userdata
        todo = [p for p in plan if p[0] not in manifest['parts']]
        self.begin(sum(p[3] for p in plan), 'Backing up')
        self.advance(sum(p[3] for p in plan if p[0] in manifest['parts']))
        for name, area, offset, size in todo:
            self.rep.status(f'Backing up {name} ({fmt_size(size)})')
            file = name + '.img.gz'
            tmp = os.path.join(folder, file + '.part')
            h = hashlib.sha256()
            with open(tmp, 'wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) as gz:
                for data in self.read(area, offset, size):
                    h.update(data)
                    gz.write(data)
            digest = h.hexdigest()
            if file_sha(tmp, gz=True) != digest:  # what reached the disk, decompressed
                os.remove(tmp)
                raise StepError(f'The backup of {name} did not save correctly to this computer.',
                                hint='Check the disk, then try again; finished parts are kept.')
            os.replace(tmp, os.path.join(folder, file))
            manifest['parts'][name] = {'area': area, 'offset': offset, 'size': size,
                                       'sha256': digest, 'file': file}
            write_json(path, manifest)
        manifest['complete'] = True
        write_json(path, manifest)
        self.rep.log(f'backup complete: {folder}')
        return folder

    # -- 3. unlock
    def unlock(self):
        if self.info.lock == 'unlocked':
            self.rep.status('The bootloader is already unlocked')
            return False
        self.begin(1, 'Unlocking the bootloader')
        ok, message = self.dev.unlock()
        seccfg = self.dev.read('user', PLACE['seccfg'][0], 512)
        if not ok or r1.lock_state(seccfg) != 'unlocked':
            raise StepError(f'Unlocking did not work ({message}).', safe=r1.lock_state(seccfg) != 'unknown',
                            hint='Run the step again. If it keeps failing, restore the backup.')
        self.info.lock = 'unlocked'
        self.advance(1)
        return True

    # -- 4. install
    def install_plan(self, pkg, mode):
        """[(label, partition, bytes or callable, length)] in the order they are written. The
        callable yields (offset, bytes) runs, for super."""
        z = lambda n: bytes(n)
        ops = []
        if mode == 'clean':
            if self.info.slot == 'b':
                for name in r1.SLOT_FIRMWARE:
                    ops.append((f'{name} (from slot B)', name + '_a', ('copy', name + '_b'), None))
            ops += [('the metadata partition', 'md_udc', z, None), ('old encryption keys', 'metadata', z, None),
                    ('your data', 'userdata', z, 64 << 20), ('vendor_boot', 'vendor_boot_a', z, None),
                    ('init_boot', 'init_boot_a', z, None), ('dtbo', 'dtbo_a', pkg.read('dtbo'), None)]
        ops += [('Android (super)', 'super', 'sparse', None),
                ('vbmeta', 'vbmeta_a', pkg.read('vbmeta'), None),
                ('the haretic bootloader (LK)', 'lk_a', ('lk', pkg.read('lk')), None),
                ('the boot logo', 'logo', pkg.read('logo'), 'image'),
                ('the kernel (boot)', 'boot_a', pkg.read('boot'), None),
                ('the boot record (slot A)', 'para', 'record', None)]
        return ops

    def install(self, pkg, mode):
        if mode not in ('clean', 'update'):
            raise ValueError(mode)
        if mode == 'update' and (self.info.slot != 'a' or self.info.lk['a'] != 'haretic'):
            raise StepError('This r1 does not run haretic, so it needs a clean install.')
        if self.info.lock != 'unlocked':
            raise StepError('The bootloader is still locked.')
        ops = self.install_plan(pkg, mode)
        with pkg.open('super') as f:
            super_bytes = sparse.written_bytes(f)
        total = 0
        for _label, part, src, length in ops:
            size = self.info.parts[part][1]
            total += 2 * (super_bytes if src == 'sparse' else 32 if src == 'record' else
                          len(src) if length == 'image' else length or size)
        self.begin(total, 'Installing')
        for label, part, src, length in ops:
            offset, size = self.info.parts[part]
            self.rep.status(f'Writing {label}')
            self.rep.log(f'write {part}')
            if src == 'sparse':
                with pkg.open('super') as f:
                    for at, data in sparse.runs(f, PIECE):
                        self.write(offset + at, data, label)
            elif src == 'record':
                rec = self.dev.read('user', offset + r1.RECORD_OFFSET, 32)
                self.write(offset + r1.RECORD_OFFSET, r1.activate_a(rec), label)
            elif isinstance(src, tuple) and src[0] == 'copy':
                for i, data in enumerate(self.read('user', PLACE[src[1]][0], size)):
                    self.total += len(data)  # the read is extra work over the plan
                    self.write(offset + i * PIECE, data, label)
            elif isinstance(src, tuple) and src[0] == 'lk':
                old = self.dev.read('user', offset, size)
                self.write(offset, src[1] + old[len(src[1]):], label)
            elif callable(src):
                n = length or size
                for at in range(0, n, PIECE):
                    self.write(offset + at, bytes(min(PIECE, n - at)), label)
            else:
                data = src if length == 'image' else src + bytes(size - len(src))
                self.write(offset, data, label)
        self.rep.log('install complete')

    # -- 5. boot
    def boot(self):
        self.rep.status('Starting haretic')
        self.dev.reboot()

    # -- restore
    def restore(self, folder):
        manifest = json.load(open(os.path.join(folder, 'manifest.json')))
        if manifest['device']['cid'] != self.info.identity['cid']:
            raise StepError('This backup is from a different r1.')
        if not manifest.get('complete'):
            raise StepError('This backup is unfinished.')
        skip = {'gpt', 'gpt_backup', 'boot1', 'boot2'} | set(r1.NEVER_WRITTEN)
        names = [n for n, p in manifest['parts'].items() if n not in skip and p['area'] == 'user']
        last = [n for n in ('boot_a', 'boot_b', 'para') if n in names]
        names = [n for n in names if n not in last] + last
        self.begin(sum(2 * manifest['parts'][n]['size'] for n in names), 'Restoring')
        for name in names:
            p = manifest['parts'][name]
            if PLACE.get(name, (p['offset'], p['size'])) != (p['offset'], p['size']) \
                    and name not in self.info.parts:
                raise StepError(f'The backup of {name} does not fit this r1.')
            self.rep.status(f'Restoring {name}')
            h = hashlib.sha256()
            with gzip.open(os.path.join(folder, p['file']), 'rb') as f:
                at = 0
                while data := f.read(PIECE):
                    h.update(data)
                    self.write(p['offset'] + at, data, name)
                    at += len(data)
            if h.hexdigest() != p['sha256']:
                raise StepError(f'The backup of {name} is damaged.', safe=False,
                                hint='Copy the backup folder again from where you keep it.')
        self.rep.log('restore complete')


def unfinished_backup(root, cid):
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else ():
        m = read_manifest(os.path.join(root, name))
        if m and m['device']['cid'] == cid and not m.get('complete'):
            return os.path.join(root, name)
    return None


def backups(root, cid=None):
    """Complete backups under root: [(folder, manifest)], newest first."""
    found = []
    for name in os.listdir(root) if os.path.isdir(root) else ():
        m = read_manifest(os.path.join(root, name))
        if m and m.get('complete') and (cid is None or m['device']['cid'] == cid):
            found.append((os.path.join(root, name), m))
    return sorted(found, key=lambda f: f[1]['created'], reverse=True)


def read_manifest(folder):
    try:
        with open(os.path.join(folder, 'manifest.json')) as f:
            m = json.load(f)
        return m if m.get('kind') == 'r1-backup' else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    with open(path + '.tmp', 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(path + '.tmp', path)


def file_sha(path, gz=False):
    h = hashlib.sha256()
    with (gzip.open if gz else open)(path, 'rb') as f:
        while data := f.read(PIECE):
            h.update(data)
    return h.hexdigest()


def fmt_size(n):
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if n < 1000 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1000
