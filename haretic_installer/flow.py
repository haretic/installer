"""The installer's steps, without a UI: check the r1, back it up, unlock it, install haretic, boot.
Each step reports through a Reporter and can be stopped between pieces; every write is read back.

A backup is read into a folder of gzipped partitions, which a pulled cable can resume, then packed
into one .tar.xz and checked: the restore file. The one made of rabbitOS is the only way back.
"""
import collections
import datetime
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

from . import r1, sparse
from .device import Cancelled, PIECE

PLACE = {n: (o, s) for n, o, s in r1.LAYOUT}
GPT_BYTES = 34 * r1.SECTOR
BACKUP_FREE = 12 << 30  # rabbitOS backs up to about 4-6 GB; leave room
XZ_BLOCK = 32 << 20
XZ_PRESET = 6  # 9 needs 674 MB per thread
XZ_THREADS = max(1, min(8, (os.cpu_count() or 2) - 1))
RESTORE_LAST = ('boot_a', 'boot_b', 'para')
VBMETA_READ = 64 << 10  # vbmeta.img is under 16 KB


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
                'system': self.system, 'installed': self.installed}


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
        slot = r1.record_slot(rec)
        installed = self.installed(slot) if lk.get(slot) == 'haretic' else None
        self.info = Info(identity=identity, parts=parts, gpt=raw, record=rec, slot=slot,
                         lock=lock, lk=lk, seccfg_type=hwtype, installed=installed)
        self.rep.log('found: ' + json.dumps(self.info.summary()))
        return self.info

    def installed(self, slot='a'):
        """The haretic build on the slot in use (B after an update over the air), from its signed
        vbmeta: {version, aosp, gapps, root}, each None where it can't tell. The build's own
        properties give the version, style and Google apps; root is whether boot still matches the
        hash vbmeta signed, as Magisk patches it."""
        self.rep.status('Reading which haretic is installed')
        slot = slot if slot in ('a', 'b') else 'a'
        vbmeta, boot = PLACE['vbmeta_' + slot], PLACE['boot_' + slot]
        avb = avb_parse(self.dev.read('user', vbmeta[0], VBMETA_READ))
        if avb is None:
            return None
        props, hashes = avb
        root = None
        if hashes.get('boot', (0, 'none'))[1] == 'sha256' and hashes['boot'][0] <= boot[1]:
            size, _, salt, digest = hashes['boot']
            h = hashlib.sha256(salt)
            for at in range(0, size, PIECE):
                h.update(self.dev.read('user', boot[0] + at, min(PIECE, size - at)))
            root = h.digest() != digest
        style = props.get('com.haretic.style')
        if style is None:
            # builds before the haretic properties: the product in the fingerprint
            product = props.get('com.android.build.boot.fingerprint', '').split('/')[1:2]
            style = {'haretic_r1': 'haretic', 'lineage_r1': 'aosp'}.get(product[0] if product else None)
        gapps = props.get('com.haretic.gapps')
        return {'version': props.get('com.haretic.version'), 'aosp': None if style is None else style == 'aosp',
                'gapps': None if gapps is None else gapps == 'true', 'root': root}

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
        """Back up the r1 into a new or unfinished folder under root, then pack it into the restore
        file; returns the file's path."""
        cid = self.info.identity['cid']
        folder = unfinished_backup(root, cid) or os.path.join(
            root, f'r1-backup-{cid[:8]}-{datetime.datetime.now():%Y%m%d-%H%M%S}')
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
        self.rep.log(f'backup read: {folder}')
        return self.pack(folder)

    def pack(self, folder):
        """The finished backup folder as one checked .tar.xz next to it, the folder removed."""
        manifest = read_manifest(folder)
        stock = manifest['device']['system'] != 'haretic'
        name = ('r1-rabbitOS-restore-' if stock else 'r1-haretic-backup-') + \
            os.path.basename(folder).split('-', 2)[-1] + '.tar.xz'
        out = os.path.join(os.path.dirname(folder), name)
        held = sum(os.path.getsize(os.path.join(folder, p['file'])) for p in manifest['parts'].values())
        free = shutil.disk_usage(folder).free
        if free < held:
            raise StepError(f'Packing the restore file needs about {fmt_size(held)} of space, but '
                            f'{os.path.dirname(folder)} has {fmt_size(free)} free.',
                            hint='Free up space, then run the backup again; what was read is kept.')
        order = restore_order(manifest)
        names = [n for n in manifest['parts'] if n not in order] + order
        manifest['order'] = order
        manifest['format'] = 'tar'
        for n in names:
            manifest['parts'][n]['file'] = n + '.img'
        meta = json.dumps(manifest, indent=1).encode()
        total = sum(manifest['parts'][n]['size'] for n in names)
        self.begin(total, 'Packing the restore file')
        tmp = out + '.part'
        try:
            with open(tmp, 'wb') as raw:
                xz = XzBlocks(raw)
                # Not tarfile's stream mode, whose write buffer is quadratic in the piece size
                with tarfile.open(fileobj=xz, mode='w', format=tarfile.PAX_FORMAT,
                                  copybufsize=PIECE) as tar:
                    add(tar, 'manifest.json', len(meta), iter([meta]))
                    for n in names:
                        self.rep.status(f'Packing the restore file: {n}')
                        with gzip.open(os.path.join(folder, n + '.img.gz'), 'rb') as f:
                            add(tar, n + '.img', manifest['parts'][n]['size'], self.pieces(f))
                xz.close()
            self.check_archive(tmp, 'Checking the restore file')
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        os.replace(tmp, out)
        shutil.rmtree(folder)
        self.rep.log(f'backup complete: {out}')
        return out

    def pieces(self, f):
        while data := f.read(PIECE):
            self.check()
            self.advance(len(data))
            yield data

    def check_archive(self, path, text):
        """Every partition in a restore file decompressed and hashed; returns its manifest."""
        try:
            with lzma.open(path) as raw, tarfile.open(fileobj=raw, mode='r|', bufsize=1 << 20) as tar:
                manifest = None
                seen = set()
                for member in tar:
                    f = tar.extractfile(member)
                    if manifest is None:
                        if member.name != 'manifest.json':
                            raise ValueError('no manifest')
                        manifest = json.loads(f.read())
                        parts = {p['file']: n for n, p in manifest['parts'].items()}
                        self.begin(sum(p['size'] for p in manifest['parts'].values()), text)
                        continue
                    n = parts.get(member.name)
                    if n is None or member.size != manifest['parts'][n]['size']:
                        raise ValueError(f'unexpected {member.name}')
                    h = hashlib.sha256()
                    for data in self.pieces(f):
                        h.update(data)
                    if h.hexdigest() != manifest['parts'][n]['sha256']:
                        raise ValueError(f'{n} differs')
                    seen.add(n)
        except (OSError, EOFError, ValueError, KeyError, tarfile.TarError, lzma.LZMAError) as e:
            raise StepError(f'The restore file {os.path.basename(path)} is damaged ({e}).',
                            hint='Copy it again from where you keep it.')
        if manifest is None or seen != set(manifest['parts']):
            raise StepError(f'The restore file {os.path.basename(path)} is incomplete.',
                            hint='Copy it again from where you keep it.')
        return manifest

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
                ('the boot logo', 'logo', pkg.read('logo'), 'image')]
        # Slot B made ready too: an update over the air installs to the other slot and boots it,
        # which needs the same firmware, the haretic LK and the stock DTBO there, and no
        # vendor_boot or init_boot for LK to take instead of boot
        for name in r1.SLOT_FIRMWARE:
            ops.append((f'{name} (slot B, as slot A)', name + '_b', ('copy', name + '_a'), None))
        ops += [('the haretic bootloader (LK, slot B)', 'lk_b', ('lk', pkg.read('lk')), None),
                ('dtbo (slot B)', 'dtbo_b', pkg.read('dtbo'), None),
                ('vendor_boot (slot B)', 'vendor_boot_b', z, None),
                ('init_boot (slot B)', 'init_boot_b', z, None),
                ('the kernel (boot)', 'boot_a', pkg.read('boot'), None),
                ('the boot record (slot A)', 'para', 'record', None)]
        return ops

    def install(self, pkg, mode, allow_downgrade=False):
        if mode not in ('clean', 'update'):
            raise ValueError(mode)
        if mode == 'update' and self.info.lk[self.info.slot] != 'haretic':
            raise StepError('This r1 does not run haretic, so it needs a clean install.')
        if mode == 'update':
            for kind, text in update_problems(self.info.installed, pkg):
                if kind == 'refuse' or kind == 'downgrade' and not allow_downgrade:
                    raise StepError(text + ' Nothing was changed.')
                self.rep.log(f'update: {text}')
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
                # tee_a is 5 MiB and tee_b 8 MiB: no more than the source holds
                for i, data in enumerate(self.read('user', PLACE[src[1]][0], min(size, PLACE[src[1]][1]))):
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
    def restore(self, path):
        """Write a restore file back: the whole file is checked first, then written in its order,
        the boot images and the boot record last."""
        manifest = archive_manifest(path)
        if manifest is None:
            raise StepError('That is not an r1 restore file.')
        if manifest['device']['cid'] != self.info.identity['cid']:
            raise StepError('This restore file is from a different r1.')
        order = restore_order(manifest)
        if manifest.get('order') != order:
            raise StepError('This restore file was not made by this installer.')
        for name in order:
            p = manifest['parts'][name]
            if PLACE.get(name, (p['offset'], p['size'])) != (p['offset'], p['size']) \
                    and name not in self.info.parts:
                raise StepError(f'The backup of {name} does not fit this r1.')
        self.check_archive(path, 'Checking the restore file')
        self.begin(sum(2 * manifest['parts'][n]['size'] for n in order), 'Restoring')
        files = {manifest['parts'][n]['file']: n for n in order}
        with lzma.open(path) as raw, tarfile.open(fileobj=raw, mode='r|', bufsize=1 << 20) as tar:
            for member in tar:
                name = files.get(member.name)
                if name is None:
                    continue
                p = manifest['parts'][name]
                self.rep.status(f'Restoring {name}')
                f = tar.extractfile(member)
                at = 0
                while data := f.read(PIECE):
                    self.write(p['offset'] + at, data, name)
                    at += len(data)
        self.rep.log('restore complete')


class XzBlocks:
    """A file object that xz-compresses what is written in independent blocks on several threads
    and writes them in order: one .xz of concatenated streams, as xz -T makes, which xz, 7-Zip
    and lzma.open all read."""

    def __init__(self, out):
        self.out = out
        self.buf = bytearray()
        self.pool = ThreadPoolExecutor(XZ_THREADS)
        self.pending = collections.deque()
        self.written = 0

    def tell(self):
        return self.written

    def write(self, data):
        self.written += len(data)
        self.buf += data
        while len(self.buf) >= XZ_BLOCK:
            self.submit(bytes(self.buf[:XZ_BLOCK]))
            del self.buf[:XZ_BLOCK]
        return len(data)

    def submit(self, block):
        self.pending.append(self.pool.submit(lzma.compress, block, preset=XZ_PRESET))
        while len(self.pending) > 2 * XZ_THREADS:
            self.out.write(self.pending.popleft().result())

    def close(self):
        if self.buf:
            self.submit(bytes(self.buf))
            self.buf.clear()
        while self.pending:
            self.out.write(self.pending.popleft().result())
        self.pool.shutdown()


class _Chunks:
    """An iterator of byte strings read as a file, without copying what is left over."""

    def __init__(self, chunks):
        self.chunks, self.cur, self.pos = chunks, b'', 0

    def read(self, n):
        out = []
        while n > 0:
            if self.pos == len(self.cur):
                self.cur, self.pos = next(self.chunks, None), 0
                if self.cur is None:
                    self.cur = b''
                    break
            take = self.cur[self.pos:self.pos + n]
            self.pos += len(take)
            n -= len(take)
            out.append(take)
        return b''.join(out)


def add(tar, name, size, chunks):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    tar.addfile(info, _Chunks(chunks))


def avb_parse(data):
    """A vbmeta image's properties and hash descriptors: ({key: value}, {partition: (image size,
    algorithm, salt, digest)}); None if it is not one."""
    if len(data) < 256 or data[:4] != b'AVB0':
        return None
    auth, aux = struct.unpack_from('>QQ', data, 12)
    at, size = struct.unpack_from('>QQ', data, 96)
    at += 256 + auth
    end = at + size
    if size > aux or end > len(data):
        return None
    props, hashes = {}, {}
    while at + 16 <= end:
        tag, n = struct.unpack_from('>QQ', data, at)
        body = data[at + 16:at + 16 + n]
        if tag == 0 and len(body) >= 16:
            kn, vn = struct.unpack_from('>QQ', body)
            props[body[16:16 + kn].decode('utf-8', 'replace')] = body[17 + kn:17 + kn + vn].decode('utf-8', 'replace')
        elif tag == 2 and len(body) >= 116:
            image, algo = struct.unpack_from('>Q32s', body)
            pn, sn, dn = struct.unpack_from('>III', body, 40)
            name = body[116:116 + pn].decode('utf-8', 'replace')
            salt = body[116 + pn:116 + pn + sn]
            hashes[name] = (image, algo.rstrip(b'\0').decode(), salt, body[116 + pn + sn:116 + pn + sn + dn])
        at += 16 + n
    return props, hashes


def version_date(version):
    m = re.match(r'^haretic-(\d{8})-', version or '')
    return m and m.group(1)


def update_problems(installed, pkg):
    """What goes wrong if pkg updates the installed haretic and keeps data: [(kind, text)], kind
    'refuse' (needs a clean install), 'downgrade' (refused unless allowed) or 'warn'."""
    if not installed:
        return []
    out = []
    name = {True: 'plain LineageOS (aosp)', False: 'haretic'}
    if installed['aosp'] is not None and installed['aosp'] != pkg.aosp:
        out.append(('refuse', f'This r1 runs {name[installed["aosp"]]} and the package is {name[pkg.aosp]}. '
                              'They cannot share apps and data, so changing between them needs a clean install.'))
    old, new = version_date(installed['version']), version_date(pkg.version)
    if old and new and new < old:
        out.append(('downgrade', f'This r1 runs {installed["version"]}, newer than {pkg.version}. Going '
                                 'back while keeping data can leave settings and apps the older version '
                                 'cannot read, and the r1 may not start. A clean install of it is safe.'))
    if installed['gapps'] is not None and installed['gapps'] != pkg.gapps:
        out.append(('warn', 'Google apps are removed: apps that need them stop working.' if installed['gapps']
                    else 'Google apps are added. Apps installed before may need signing in again.'))
    if installed['root'] is not None and installed['root'] != pkg.root:
        out.append(('warn', 'Root (Magisk) is removed: apps that need it stop working, and Magisk modules '
                            'stay but do nothing.' if installed['root'] else 'Root (Magisk) is added.'))
    return out


def restore_order(manifest):
    """The partitions a restore writes, in order: the boot images and the boot record last."""
    skip = {'gpt', 'gpt_backup', 'boot1', 'boot2'} | set(r1.NEVER_WRITTEN)
    names = [n for n, p in manifest['parts'].items() if n not in skip and p['area'] == 'user']
    last = [n for n in RESTORE_LAST if n in names]
    return [n for n in names if n not in last] + last


def archive_manifest(path):
    """The manifest at the front of a restore file, or None."""
    try:
        with lzma.open(path) as raw, tarfile.open(fileobj=raw, mode='r|', bufsize=1 << 20) as tar:
            member = tar.next()
            if member is None or member.name != 'manifest.json':
                return None
            m = json.loads(tar.extractfile(member).read())
        return m if m.get('kind') == 'r1-backup' and m.get('complete') else None
    except (OSError, EOFError, ValueError, tarfile.TarError, lzma.LZMAError):
        return None


def unfinished_backup(root, cid):
    """A backup folder of this r1 still being read or packed."""
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else ():
        m = read_manifest(os.path.join(root, name))
        if m and m['device']['cid'] == cid:
            return os.path.join(root, name)
    return None


def backups(root, cid=None, system=None):
    """Restore files under root: [(path, manifest)], newest first; system 'rabbitOS' or 'haretic'
    picks what was on the r1."""
    found = []
    for name in os.listdir(root) if os.path.isdir(root) else ():
        if not name.endswith('.tar.xz'):
            continue
        m = archive_manifest(os.path.join(root, name))
        if m and (cid is None or m['device']['cid'] == cid) \
                and (system is None or m['device']['system'] == system):
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
