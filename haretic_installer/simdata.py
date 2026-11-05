"""The simulated r1's storage: the r1's GPT layout with random GUIDs, a boot record, a seccfg and
a tag in every partition. No rabbit or haretic binaries."""
import os
import struct
import uuid
import zlib

from . import r1

DATA_GUID = uuid.UUID('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7')
USER_SIZE = 0x1d1ec00000


def gpt(user_size=USER_SIZE):
    parts = list(r1.LAYOUT) + [('userdata', r1.USERDATA_START, 0x1adc0f8000),
                               ('otp', 0x1d1b0f8000, 0x2b00000), ('flashinfo', 0x1d1dbf8000, 0x1000000)]
    table = bytearray(128 * 128)
    for i, (name, offset, size) in enumerate(parts):
        struct.pack_into('<16s16sQQQ72s', table, i * 128, DATA_GUID.bytes_le, uuid.uuid4().bytes_le,
                         offset // 512, (offset + size) // 512 - 1, 0, name.encode('utf-16le'))
    last = user_size // 512 - 1
    header = bytearray(struct.pack('<8sIIIIQQQQ16sQIII', b'EFI PART', 0x10000, 92, 0, 0, 1, last,
                                   34, last - 33, uuid.uuid4().bytes_le, 2, 128, 128, zlib.crc32(table)))
    struct.pack_into('<I', header, 16, zlib.crc32(bytes(header)))
    mbr = bytearray(512)
    mbr[446:462] = struct.pack('<BBBBBBBBII', 0, 0, 2, 0, 0xee, 0xff, 0xff, 0xff, 1, min(last, 0xffffffff))
    mbr[510:512] = b'\x55\xaa'
    return bytes(mbr) + bytes(header) + bytes(420) + bytes(table)


def record(slot):
    rec = bytearray(32)
    rec[0:2] = b'_' + slot.encode()
    rec[4:10] = b'BCAB\x01\x02'
    a, b = (0x9f, 0x7e) if slot == 'a' else (0x7e, 0x9f)
    rec[12], rec[14] = a, b
    return r1.seal(rec)


def seccfg(locked):
    head = struct.pack('<IIIIIII', 0x4D4D4D4D, 4, 0x3c, 1 if locked else 3, 0, 0, 0x45454545)
    return head + os.urandom(32)


def contents(slot='a', locked=True, haretic=False):
    """(offset, bytes) to write into a blank simulated r1."""
    place = {n: o for n, o, _ in r1.LAYOUT}
    yield 0, gpt()
    for name, offset, size in r1.LAYOUT:
        yield offset, (f'sim {name} ' * 8).encode()[:min(size, 256)]
    yield place['para'] + r1.RECORD_OFFSET, record(slot)
    yield place['seccfg'], seccfg(locked)
    yield r1.USERDATA_START, b'sim userdata'
