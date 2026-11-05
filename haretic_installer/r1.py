"""What the installer knows about the rabbit r1: its USB IDs, eMMC layout, boot record, lock state
and loaders. Pure functions over bytes; no USB I/O."""
import hashlib
import struct
import zlib

VENDOR = 0x0E8D
# The r1's MediaTek USB functions: boot ROM, preloader ("MT65xx Preloader", a CDC ACM port), and
# the download agent once it runs. The Windows driver check and the device scan use exactly these.
USB_IDS = {0x0003: 'boot ROM', 0x2000: 'preloader', 0x2001: 'download agent'}
HW_CODE = 0x766  # MT6765
SECTOR = 512
BOOT_AREA = 4 * 1024 * 1024  # boot1 and boot2: the two preloader copies

# (name, offset, size) of every partition whose place is fixed on the r1; the last three follow
# the eMMC's size, so only userdata's start is fixed.
LAYOUT = (
    ('boot_para', 0x8000, 0x100000), ('para', 0x108000, 0x80000), ('expdb', 0x188000, 0x1400000),
    ('frp', 0x1588000, 0x100000), ('nvcfg', 0x1688000, 0x2000000), ('nvdata', 0x3688000, 0x4000000),
    ('md_udc', 0x7688000, 0x169a000), ('metadata', 0x8d22000, 0x2000000),
    ('protect1', 0xad22000, 0x800000), ('protect2', 0xb522000, 0xade000),
    ('seccfg', 0xc000000, 0x800000), ('sec1', 0xc800000, 0x200000), ('proinfo', 0xca00000, 0x300000),
    ('nvram', 0xcd00000, 0x4000000), ('logo', 0x10d00000, 0xb00000),
    ('md1img_a', 0x11800000, 0x6400000), ('spmfw_a', 0x17c00000, 0x100000),
    ('scp_a', 0x17d00000, 0x100000), ('sspm_a', 0x17e00000, 0x100000), ('gz_a', 0x17f00000, 0x1000000),
    ('lk_a', 0x18f00000, 0x100000), ('boot_a', 0x19000000, 0x2000000),
    ('vendor_boot_a', 0x1b000000, 0x4000000), ('init_boot_a', 0x1f000000, 0x800000),
    ('dtbo_a', 0x1f800000, 0x800000), ('tee_a', 0x20000000, 0x500000), ('vbmeta_a', 0x20500000, 0x800000),
    ('vbmeta_system_a', 0x20d00000, 0x800000), ('vbmeta_vendor_a', 0x21500000, 0xb00000),
    ('md1img_b', 0x22000000, 0x6400000), ('spmfw_b', 0x28400000, 0x100000),
    ('scp_b', 0x28500000, 0x100000), ('sspm_b', 0x28600000, 0x100000), ('gz_b', 0x28700000, 0x1000000),
    ('lk_b', 0x29700000, 0x100000), ('boot_b', 0x29800000, 0x2000000),
    ('vendor_boot_b', 0x2b800000, 0x4000000), ('init_boot_b', 0x2f800000, 0x800000),
    ('dtbo_b', 0x30000000, 0x800000), ('tee_b', 0x30800000, 0x800000), ('super', 0x31000000, 0x20c0c4000),
    ('vbmeta_b', 0x23d0c4000, 0x800000), ('vbmeta_system_b', 0x23d8c4000, 0x800000),
    ('vbmeta_vendor_b', 0x23e0c4000, 0xf3c000),
)
USERDATA_START = 0x23f000000
TAIL = ('userdata', 'otp', 'flashinfo')

# Never written by the installer, whatever it installs or restores: the preloaders (outside the
# GPT, in boot1/boot2) keep the r1 reachable over USB, and these hold its calibration and IDs.
NEVER_WRITTEN = ('nvcfg', 'nvdata', 'protect1', 'protect2', 'proinfo', 'nvram', 'otp', 'flashinfo')

# The firmware a slot boots with besides Android: copied from B when a slot-B r1 moves to A.
SLOT_FIRMWARE = ('md1img', 'spmfw', 'scp', 'sspm', 'gz', 'tee')

LK_PAYLOAD = 864000
LK_STOCK = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'  # rabbitOS v0.8.293
LK_HARETIC = {'9cf223eb1829294f3f8b6e52fdfaec291a76072289ddba09a7cb8248d417ffb1',
              'e4299344da5d5dd1a202b4964de764c43557f97203799e77bab55ac962973de0'}

RECORD_OFFSET = 2048  # the A/B boot record in para, as the preloader, LK and Android read it
_SLOTS = 12


class NotAnR1(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def parse_gpt(data):
    """Partitions of a primary GPT (protective MBR, header and entries): {name: (offset, size)}."""
    if len(data) < 2 * SECTOR or data[SECTOR:SECTOR + 8] != b'EFI PART':
        raise NotAnR1('The storage has no GPT partition table.')
    header = data[SECTOR:SECTOR + 92]
    size = struct.unpack_from('<I', header, 12)[0]
    want = struct.unpack_from('<I', header, 16)[0]
    if zlib.crc32(header[:16] + b'\0' * 4 + header[20:size]) != want:
        raise NotAnR1('The partition table is damaged (header checksum).')
    first, count, entry = struct.unpack_from('<QII', header, 72)
    table = data[first * SECTOR:first * SECTOR + count * entry]
    if len(table) != count * entry or zlib.crc32(table) != struct.unpack_from('<I', header, 88)[0]:
        raise NotAnR1('The partition table is damaged (entry checksum).')
    parts = {}
    for i in range(count):
        e = table[i * entry:(i + 1) * entry]
        if e[:16] == bytes(16):
            continue
        lo, hi = struct.unpack_from('<QQ', e, 32)
        name = e[56:128].decode('utf-16le', 'replace').rstrip('\0')
        parts[name] = (lo * SECTOR, (hi - lo + 1) * SECTOR)
    return parts


def check_layout(parts):
    """Raise NotAnR1 unless the partitions are the r1's, in its places."""
    for name, offset, size in LAYOUT:
        if name not in parts:
            raise NotAnR1(f'It has no "{name}" partition, which every r1 has.')
        if parts[name] != (offset, size):
            raise NotAnR1(f'Its "{name}" partition is not where an r1 has it.')
    if parts.get('userdata', (0, 0))[0] != USERDATA_START:
        raise NotAnR1('Its "userdata" partition is not where an r1 has it.')
    extra = set(parts) - {n for n, _, _ in LAYOUT} - set(TAIL)
    if extra:
        raise NotAnR1('It has partitions an r1 does not: ' + ', '.join(sorted(extra)))


# The boot record: 32 bytes, "BCAB" at 4, version 1 at 8, the slot count in byte 9, two bytes per
# slot from 12 (priority in the low nibble, tries left in bits 4-6, successful in bit 7), CRC-32 of
# the first 28 bytes at 28, little endian.
def record_valid(rec):
    return (len(rec) == 32 and rec[4:8] == b'BCAB' and rec[8] == 1 and rec[9] & 7 == 2
            and struct.unpack_from('<I', rec, 28)[0] == zlib.crc32(bytes(rec[:28])))


def seal(rec):
    rec = bytearray(rec)
    struct.pack_into('<I', rec, 28, zlib.crc32(bytes(rec[:28])))
    return bytes(rec)


def record_slot(rec):
    """The slot the r1 boots: 'a' or 'b', the higher priority, the one it last booted on a tie."""
    suffix = bytes(rec[:2])
    current = 1 if suffix == b'_b' else 0
    a, b = rec[_SLOTS] & 15, rec[_SLOTS + 2] & 15
    slot = current
    if a > (rec[_SLOTS + 2 * slot] & 15):
        slot = 0
    if b > (rec[_SLOTS + 2 * slot] & 15):
        slot = 1
    return 'ab'[slot]


def activate_a(rec):
    """The record with slot A active, as the r1's boot HAL sets it (device/rabbit/r1 boot
    Control::SetActive): A at priority 15 with 6 tries, keeping its successful bit; B, if also
    at 15, drops to 14. Every other byte is kept."""
    if not record_valid(rec):
        raise ValueError('The r1 boot record is not valid; refusing to change it.')
    nxt = bytearray(rec)
    current = 1 if bytes(rec[:2]) == b'_b' else 0
    if nxt[_SLOTS + 2] & 15 == 15:
        nxt[_SLOTS + 2] = (nxt[_SLOTS + 2] & 0xf0) | 14
    nxt[_SLOTS] = (nxt[_SLOTS] & 0x80) | 0x6f
    if current != 0:
        nxt[_SLOTS + 1] &= 0xfe
    return seal(nxt)


# seccfg v4: "MMMM", version, size, lock state, critical lock state, runtime, "EEEE", hash.
LOCK_STATES = {1: 'locked', 2: 'locked', 3: 'unlocked', 4: 'locked', 5: 'locked'}


def lock_state(seccfg):
    if seccfg[:4] != b'MMMM' or seccfg[24:28] != b'EEEE':
        return 'unknown'
    return LOCK_STATES.get(struct.unpack_from('<I', seccfg, 12)[0], 'unknown')


def lk_kind(lk):
    h = sha(bytes(lk[:LK_PAYLOAD]))
    if h in LK_HARETIC:
        return 'haretic'
    if h == LK_STOCK:
        return 'stock'
    return 'other'
