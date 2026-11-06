"""The installer's steps against a simulated r1: check, backup, unlock, clean install from slot A
and slot B, update, restore, a cable pulled mid-backup, and not-an-r1 refusals."""
import hashlib
import json
import lzma
import os
import struct
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from haretic_installer import r1, package, sparse, simdata  # noqa: E402
from haretic_installer.device import SimDevice, DeviceError  # noqa: E402
from haretic_installer.flow import Session, Reporter, StepError, PLACE, backups, archive_manifest  # noqa: E402
from haretic_installer import flow  # noqa: E402

flow.XZ_PRESET = 0  # the simulated r1 is mostly zeros and random bytes; packing speed is not tested

LK = os.urandom(r1.LK_PAYLOAD)
PARTS = r1.parse_gpt(simdata.gpt())
r1.LK_HARETIC.add(hashlib.sha256(LK).hexdigest())


def sparse_image(path, total, runs):
    """A sparse image of `total` bytes with raw runs {offset: bytes} and a zero fill at the end."""
    block = 4096
    chunks, at = [], 0
    for off, data in sorted(runs.items()):
        if off > at:
            chunks.append(struct.pack('<HHII', sparse.DONT_CARE, 0, (off - at) // block, 12))
        chunks.append(struct.pack('<HHII', sparse.RAW, 0, len(data) // block, 12 + len(data)) + data)
        at = off + len(data)
    chunks.append(struct.pack('<HHII', sparse.FILL, 0, 4, 16) + bytes(4))
    at += 4 * block
    chunks.append(struct.pack('<HHII', sparse.DONT_CARE, 0, (total - at) // block, 12))
    with open(path, 'wb') as f:
        f.write(struct.pack('<IHHHHIIII', sparse.MAGIC, 1, 0, 28, 12, block, total // block, len(chunks), 0))
        f.write(b''.join(chunks))


def vbmeta(boot, version='haretic-20261105-1430-beta', style='haretic', gapps='false', size=65536):
    """A vbmeta image as avbtool makes it, unsigned: the haretic properties and boot's hash."""
    def desc(tag, body):
        body += bytes(-len(body) % 8)
        return struct.pack('>QQ', tag, len(body)) + body
    descs = b''
    for k, v in (('com.haretic.version', version), ('com.haretic.style', style), ('com.haretic.gapps', gapps)):
        descs += desc(0, struct.pack('>QQ', len(k), len(v)) + k.encode() + b'\0' + v.encode() + b'\0')
    salt = os.urandom(32)
    image = len(boot) - 4096
    digest = hashlib.sha256(salt + boot[:image]).digest()
    descs += desc(2, struct.pack('>Q32sIIII60s', image, b'sha256', 4, 32, 32, 0, bytes(60)) + b'boot' + salt + digest)
    aux = descs + bytes(-len(descs) % 64)
    head = struct.pack('>4sIIQQIQQQQQQQQQQQII48s80s', b'AVB0', 1, 0, 0, len(aux), 0, 0, 0, 0, 0, 0, 0, 0, 0,
                       0, len(descs), 0, 1, 0, b'avbtool 1.3.0', bytes(80))
    return head + aux + bytes(size - len(head) - len(aux))


def make_package(folder):
    boot = b'ANDROID!' + os.urandom(0x2000000 - 8)
    files = {'lk': LK, 'logo': os.urandom(1460109), 'dtbo': os.urandom(0x800000),
             'vbmeta': vbmeta(boot), 'boot': boot}
    paths = {}
    for key, name, _ in package.IMAGES:
        paths[key] = os.path.join(folder, name)
        if key != 'super':
            open(paths[key], 'wb').write(files[key])
    sparse_image(paths['super'], PLACE['super'][1], {0: os.urandom(1 << 20), 0x40000000: os.urandom(3 << 20)})
    out = os.path.join(folder, package.file_name('haretic-20261105-1430-beta', False, False))
    package.build(out, paths, 'haretic-20261105-1430-beta')
    assert package.NAME.match(os.path.basename(out))
    return package.Package(out), files


class Log(Reporter):
    def __init__(self):
        self.lines, self.last = [], None

    def status(self, text):
        self.lines.append(text)

    def progress(self, done, total):
        assert 0 <= done <= total, (done, total)
        self.last = (done, total)


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.dir = self.tmp.name
        self.pkg, self.files = make_package(self.dir)
        self.assertTrue(self.pkg.verify())
        self.assertTrue(self.pkg.checked())

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, **kw):
        dev = SimDevice(os.path.join(self.dir, 'r1'), **kw)
        s = Session(dev, Log())
        s.inspect(s.connect(lambda st: None))
        return s, dev

    def part(self, dev, name, n=None):
        o, s = PARTS[name]
        return dev.read('user', o, n or s)

    def install_checks(self, s, dev, before):
        self.assertEqual(self.part(dev, 'boot_a'), self.files['boot'])
        self.assertEqual(self.part(dev, 'lk_a', r1.LK_PAYLOAD), LK)
        self.assertEqual(self.part(dev, 'lk_a')[r1.LK_PAYLOAD:], before['lk_a'][r1.LK_PAYLOAD:])
        self.assertEqual(self.part(dev, 'vbmeta_a'), self.files['vbmeta'] + bytes(0x800000 - 65536))
        self.assertEqual(self.part(dev, 'logo', 1460109), self.files['logo'])
        self.assertEqual(self.part(dev, 'logo')[1460109:], before['logo'][1460109:])
        self.assertEqual(self.part(dev, 'userdata', 64 << 20), bytes(64 << 20))
        self.assertEqual(self.part(dev, 'md_udc'), bytes(PLACE['md_udc'][1]))
        o = PLACE['super'][0]
        sup = dev.read('user', o, 1 << 20)
        with self.pkg.open('super') as f:
            first = next(sparse.runs(f))
        self.assertEqual(sup, first[1])
        rec = dev.read('user', PLACE['para'][0] + r1.RECORD_OFFSET, 32)
        self.assertTrue(r1.record_valid(rec))
        self.assertEqual(r1.record_slot(rec), 'a')
        # slot B ready for an update over the air: slot A's firmware, the haretic LK and DTBO
        self.assertEqual(self.part(dev, 'lk_b', r1.LK_PAYLOAD), LK)
        self.assertEqual(self.part(dev, 'dtbo_b', len(self.files['dtbo'])), self.files['dtbo'])
        for name in ('vendor_boot_b', 'init_boot_b'):
            self.assertEqual(self.part(dev, name), bytes(PLACE[name][1]), name)
        for name in r1.SLOT_FIRMWARE:
            n = min(PLACE[name + '_a'][1], PLACE[name + '_b'][1])
            self.assertEqual(self.part(dev, name + '_b', n), self.part(dev, name + '_a', n), name)
        for name in r1.NEVER_WRITTEN[:6]:
            self.assertEqual(self.part(dev, name), before[name], name)
        for area in ('boot1', 'boot2'):
            self.assertTrue(dev.read(area, 0, 4).startswith(b'MMM'))

    def run_clean(self, slot):
        s, dev = self.session(slot=slot)
        self.assertEqual((s.info.slot, s.info.lock), (slot, 'locked'))
        before = {n: self.part(dev, n) for n, _, _ in r1.LAYOUT if PLACE[n][1] <= 0x6400000}
        folder = s.backup(os.path.join(self.dir, 'backups'))
        self.assertTrue(os.path.basename(folder).startswith('r1-rabbitOS-restore-15010044-'))
        self.assertEqual(os.listdir(os.path.join(self.dir, 'backups')), [os.path.basename(folder)])
        m = archive_manifest(folder)
        self.assertTrue(m['complete'])
        self.assertNotIn('userdata', m['parts'])
        self.assertEqual(m['order'][-3:], ['boot_a', 'boot_b', 'para'])
        with lzma.open(folder) as raw, tarfile.open(fileobj=raw, mode='r|') as tar:
            for member in tar:
                if member.name == 'seccfg.img':
                    self.assertEqual(tar.extractfile(member).read(), before['seccfg'])
        self.assertTrue(s.unlock())
        self.assertEqual(r1.lock_state(self.part(dev, 'seccfg', 64)), 'unlocked')
        s.install(self.pkg, 'clean')
        self.assertEqual(s.done, s.total)  # the plan counts every byte moved, tee_b's 8 MiB too
        self.install_checks(s, dev, before)
        if slot == 'b':
            for name in r1.SLOT_FIRMWARE:
                a = self.part(dev, name + '_a', PLACE[name + '_b'][1] if PLACE[name + '_b'][1] < PLACE[name + '_a'][1] else None)
                self.assertTrue(a.startswith(before[name + '_b'][:64]), name)
        return s, dev, folder, before

    def test_clean_slot_a(self):
        self.run_clean('a')

    def test_clean_slot_b(self):
        self.run_clean('b')

    def test_update_then_restore(self):
        s, dev, folder, before = self.run_clean('a')
        s2 = Session(dev, Log())
        s2.inspect(s2.connect(lambda st: None))
        self.assertEqual(s2.info.system, 'haretic')
        s2.install(self.pkg, 'update')
        self.assertEqual(self.part(dev, 'boot_a'), self.files['boot'])
        self.assertEqual(backups(os.path.join(self.dir, 'backups'), s2.info.identity['cid'])[0][0], folder)
        s2.restore(folder)
        for name, data in before.items():
            self.assertEqual(self.part(dev, name), data, name)

    def test_cable_pull_resumes(self):
        s, dev = self.session(fail_at=200 << 20)
        with self.assertRaises(DeviceError):
            s.backup(os.path.join(self.dir, 'backups'))
        done = len(json.load(open(os.path.join(self.dir, 'backups', os.listdir(os.path.join(self.dir, 'backups'))[0], 'manifest.json')))['parts'])
        self.assertGreater(done, 3)
        folder = s.backup(os.path.join(self.dir, 'backups'))
        self.assertTrue(archive_manifest(folder)['complete'])
        self.assertEqual(os.listdir(os.path.join(self.dir, 'backups')), [os.path.basename(folder)])

    def test_unfinished_backup_of_another_state_is_not_resumed(self):
        """A rabbitOS backup cut short, then haretic installed: a backup of haretic starts afresh
        rather than finishing the rabbitOS restore file with haretic's partitions."""
        s, dev = self.session(fail_at=200 << 20)
        root = os.path.join(self.dir, 'backups')
        with self.assertRaises(DeviceError):
            s.backup(root)
        stale = os.listdir(root)
        dev.write(PLACE['lk_a'][0], LK)
        s2 = Session(dev, Log())
        s2.inspect(s2.connect(lambda st: None))
        self.assertEqual(s2.info.system, 'haretic')
        own = s2.backup(root)
        self.assertTrue(os.path.basename(own).startswith('r1-haretic-backup-'))
        self.assertEqual(archive_manifest(own)['device']['system'], 'haretic')
        self.assertEqual(sorted(os.listdir(root)), sorted(stale + [os.path.basename(own)]))

    def test_damaged_restore_file_writes_nothing(self):
        s, dev, path, before = self.run_clean('a')
        raw = bytearray(open(path, 'rb').read())
        raw[len(raw) // 2] ^= 0xff
        open(path, 'wb').write(raw)
        boot = self.part(dev, 'boot_a')
        s2 = Session(dev, Log())
        s2.inspect(s2.connect(lambda st: None))
        with self.assertRaises(StepError):
            s2.restore(path)
        self.assertEqual(self.part(dev, 'boot_a'), boot)

    def test_protected_areas_are_never_written(self):
        s, dev = self.session()
        size = s.info.identity['user_size']
        for name in r1.NEVER_WRITTEN:
            o, n = s.info.parts[name]
            for at in (o - 512, o + n - 512):
                with self.assertRaises(StepError, msg=name):
                    s.write(at, bytes(1024), name)
        for at in (0, flow.GPT_BYTES - 512, size - 512):
            with self.assertRaises(StepError):
                s.write(at, bytes(512), 'gpt')
        s.write(PLACE['boot_para'][0], bytes(512), 'boot_para')  # just past the GPT

    def test_restore_refuses_a_misplaced_part(self):
        s, dev = self.session()
        s.check_archive = lambda path, text: None  # only the place is under test
        o, n = PLACE['logo']
        for place in ((o + 4096, n), (o, n + 4096)):
            parts = {'logo': {'area': 'user', 'offset': place[0], 'size': place[1], 'file': 'logo.img'}}
            m = {'kind': 'r1-backup', 'complete': True, 'device': {'cid': s.info.identity['cid']}, 'parts': parts}
            m['order'] = flow.restore_order(m)
            real, flow.archive_manifest = flow.archive_manifest, lambda path: m
            try:
                with self.assertRaises(StepError):
                    s.restore('unused')
            finally:
                flow.archive_manifest = real

    def test_haretic_backup_is_its_own(self):
        s, dev, stock, _ = self.run_clean('a')
        s2 = Session(dev, Log())
        s2.inspect(s2.connect(lambda st: None))
        own = s2.backup(os.path.join(self.dir, 'backups'))
        self.assertTrue(os.path.basename(own).startswith('r1-haretic-backup-'))
        root = os.path.join(self.dir, 'backups')
        self.assertEqual([f for f, _ in backups(root, s2.info.identity['cid'], 'rabbitOS')], [stock])

    def haretic_r1(self, boot, slot='a', **props):
        """A simulated r1 already running haretic on slot, its vbmeta made with props."""
        dev = SimDevice(os.path.join(self.dir, 'r1'), slot=slot)
        s = Session(dev, Log())
        identity = s.connect(lambda st: None)
        for name, data in (('lk_', LK), ('boot_', boot), ('vbmeta_', vbmeta(self.files['boot'], **props))):
            dev.write(PLACE[name + slot][0], data)
        s.inspect(identity)
        s.info.lock = 'unlocked'
        return s, dev

    def test_avb_matches_avbtool(self):
        avb = flow.avb_parse(self.files['vbmeta'])
        self.assertEqual(avb[0]['com.haretic.version'], 'haretic-20261105-1430-beta')
        self.assertEqual(avb[1]['boot'][:2], (0x2000000 - 4096, 'sha256'))
        self.assertIsNone(flow.avb_parse(os.urandom(4096)))

    def test_update_guards(self):
        boot = self.files['boot']
        s, dev = self.haretic_r1(boot, version='haretic-20261201-0900-nightly')
        self.assertEqual(s.info.installed, {'version': 'haretic-20261201-0900-nightly', 'aosp': False, 'gapps': False,
                                            'root': False})
        before = dev.read('user', PLACE['super'][0], 1 << 20)
        with self.assertRaises(StepError):
            s.install(self.pkg, 'update')
        self.assertEqual(dev.read('user', PLACE['super'][0], 1 << 20), before)
        s.install(self.pkg, 'update', allow_downgrade=True)
        self.assertNotEqual(dev.read('user', PLACE['super'][0], 1 << 20), before)
        # the plain LineageOS build: no override
        s, dev = self.haretic_r1(boot, style='aosp')
        with self.assertRaises(StepError):
            s.install(self.pkg, 'update', allow_downgrade=True)
        # a Magisk-patched boot and Google apps: warnings only
        patched = bytearray(boot)
        patched[0x1000] ^= 1
        s, dev = self.haretic_r1(bytes(patched), gapps='true')
        self.assertTrue(s.info.installed['root'])
        kinds = [k for k, _ in flow.update_problems(s.info.installed, self.pkg)]
        self.assertEqual(kinds, ['warn', 'warn'])
        s.install(self.pkg, 'update')

    def test_update_from_slot_b(self):
        """After an update over the air the r1 runs slot B: an update reads that build and moves
        the r1 back to slot A."""
        s, dev = self.haretic_r1(self.files['boot'], slot='b', version='haretic-20261201-0900-nightly')
        self.assertEqual(s.info.slot, 'b')
        self.assertEqual(s.info.installed['version'], 'haretic-20261201-0900-nightly')
        self.assertFalse(s.info.installed['root'])
        with self.assertRaises(StepError):
            s.install(self.pkg, 'update')
        s.install(self.pkg, 'update', allow_downgrade=True)
        self.assertEqual(self.part(dev, 'boot_a'), self.files['boot'])
        rec = dev.read('user', PLACE['para'][0] + r1.RECORD_OFFSET, 32)
        self.assertEqual(r1.record_slot(rec), 'a')

    def test_cli_downgrade(self):
        """The terminal's update refuses an older build, and --allow-downgrade lets it through."""
        from unittest import mock
        from haretic_installer import cli
        s, dev = self.haretic_r1(self.files['boot'], version='haretic-20261201-0900-nightly')
        args = ['install', self.pkg.path, '--update', '--yes', '--backups', os.path.join(self.dir, 'backups')]
        before = dev.read('user', PLACE['super'][0], 1 << 20)
        with mock.patch.object(cli, 'MtkDevice', lambda *a, **k: dev), \
                mock.patch.object(cli.paths, 'work', lambda: self.dir), \
                mock.patch.object(cli.paths, 'downloads', lambda: os.path.dirname(self.pkg.path)), \
                mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            self.assertEqual(cli.main(args), 2)
            self.assertEqual(dev.read('user', PLACE['super'][0], 1 << 20), before)
            self.assertEqual(cli.main(args + ['--allow-downgrade']), 0)
        self.assertEqual(self.part(dev, 'boot_a'), self.files['boot'])

    def test_update_refused_on_stock(self):
        s, dev = self.session()
        s.info.lock = 'unlocked'
        with self.assertRaises(StepError):
            s.install(self.pkg, 'update')

    def test_not_an_r1(self):
        s, dev = self.session()
        raw = bytearray(dev.read('user', 0, 34 * 512))
        raw[1024 + 32:1024 + 40] = struct.pack('<Q', 0x10)  # move boot_para
        parts = None
        with self.assertRaises(r1.NotAnR1):
            parts = r1.parse_gpt(bytes(raw))
        hdr = raw[512:604]
        table = bytes(raw[1024:1024 + 128 * 128])
        import zlib
        struct.pack_into('<I', hdr, 88, zlib.crc32(table))
        struct.pack_into('<I', hdr, 16, 0)
        struct.pack_into('<I', hdr, 16, zlib.crc32(bytes(hdr)))
        raw[512:604] = hdr
        with self.assertRaises(r1.NotAnR1):
            r1.check_layout(r1.parse_gpt(bytes(raw)))
        self.assertIsNone(parts)
        r1.check_layout(r1.parse_gpt(simdata.gpt()))


class Names(unittest.TestCase):
    """Package names and versions, with and without the build time."""

    def test_version_date(self):
        vd = flow.version_date
        self.assertEqual(vd('haretic-20261105-1430-nightly'), ('20261105', '1430'))
        self.assertEqual(vd('haretic-20261105-nightly'), ('20261105', ''))
        self.assertEqual(vd('haretic-20261105-1430-beta-aosp'), ('20261105', '1430'))
        for v in (None, '', 'lineage-24.0-20261105', 'haretic-2026110-nightly'):
            self.assertFalse(vd(v), v)
        # an r1 installed before the time was added: any build of that day or later is newer
        self.assertLess(vd('haretic-20261105-nightly'), vd('haretic-20261105-0001-nightly'))
        self.assertLess(vd('haretic-20261104-2359-nightly'), vd('haretic-20261105-nightly'))
        self.assertLess(vd('haretic-20261105-0959-nightly'), vd('haretic-20261105-1000-nightly'))

    def test_update_problems_old_versions(self):
        pkg = type('P', (), {'aosp': False, 'gapps': False, 'root': False})
        installed = {'version': 'haretic-20261105-nightly', 'aosp': False, 'gapps': False, 'root': False}
        pkg.version = 'haretic-20261105-0900-nightly'
        self.assertEqual(flow.update_problems(installed, pkg), [])
        pkg.version = 'haretic-20261104-2300-nightly'
        self.assertEqual([k for k, _ in flow.update_problems(installed, pkg)], ['downgrade'])
        installed['version'] = None  # builds before the haretic properties: no check
        self.assertEqual(flow.update_problems(installed, pkg), [])

    def test_name(self):
        for gapps in (False, True):
            for root in (False, True):
                for aosp in (False, True):
                    for version in ('haretic-20261106-0130-nightly', 'haretic-20261105-beta'):
                        name = package.file_name(version, gapps, root, aosp)
                        m = package.NAME.match(name)
                        self.assertTrue(m, name)
                        self.assertEqual((m['version'], bool(m['aosp']), m['apps'] == 'g', m['root'] == 'S'),
                                         (version, aosp, gapps, root))
        self.assertEqual(package.file_name('haretic-20261106-0130-release', True, False, True),
                         'haretic-20261106-0130-release-aosp-r1_bgN.tar.xz')
        for bad in ('haretic-20261106-0130-nightly-r1_bvN.tar.gz', 'haretic-20261106-0130-nightly-r1_avN.tar.xz',
                    'haretic-20261106-0130-nightly-r1_bvN-ota.zip', 'haretic-20261106-0130-nightly-r1_bvN.tar.xz.part',
                    'haretic-20261106-130-nightly-r1_bvN.tar.xz', 'haretic-20261106-0130-Nightly-r1_bvN.tar.xz'):
            self.assertIsNone(package.NAME.match(bad), bad)


if __name__ == '__main__':
    unittest.main()
