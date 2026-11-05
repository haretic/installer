"""Android sparse images, read as runs of bytes to write; what they leave out is not written."""
import struct
import zlib

MAGIC = 0xED26FF3A
RAW, FILL, DONT_CARE, CRC = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


class SparseError(ValueError):
    pass


def is_sparse(head):
    return len(head) >= 4 and struct.unpack_from('<I', head)[0] == MAGIC


def _read(stream, n):
    data = stream.read(n)
    if len(data) != n:
        raise SparseError('The image ends early.')
    return data


def header(stream):
    """(block size, total bytes, chunk count) of a sparse image, stream positioned after it."""
    h = _read(stream, 28)
    magic, major, _minor, hsize, csize, block, blocks, chunks, _crc = struct.unpack('<IHHHHIIII', h)
    if magic != MAGIC or major != 1 or hsize < 28 or csize < 12 or block % 4:
        raise SparseError('Not an Android sparse image.')
    _read(stream, hsize - 28)
    return block, blocks * block, chunks, csize


def runs(stream, piece=16 << 20):
    """Yield (offset, bytes) to write, at most `piece` bytes each, for a sparse image. Raw and
    fill chunks are written; don't-care ranges keep what the partition has. The image's CRC
    chunks, if any, are checked."""
    block, total, chunks, csize = header(stream)
    offset = 0
    crc = 0
    for _ in range(chunks):
        kind, _r, count, size = struct.unpack('<HHII', _read(stream, 12))
        _read(stream, csize - 12)
        length = count * block
        if kind == RAW:
            if size - csize != length:
                raise SparseError('A raw chunk has the wrong size.')
            done = 0
            while done < length:
                data = _read(stream, min(piece, length - done))
                crc = zlib.crc32(data, crc)
                yield offset + done, data
                done += len(data)
        elif kind == FILL:
            if size - csize != 4:
                raise SparseError('A fill chunk has the wrong size.')
            pattern = _read(stream, 4)
            done = 0
            while done < length:
                n = min(piece, length - done)
                data = pattern * (n // 4)
                crc = zlib.crc32(data, crc)
                yield offset + done, data
                done += n
        elif kind == DONT_CARE:
            if size != csize:
                raise SparseError('A skip chunk has data.')
            # The CRC covers skipped blocks as zeros
            for done in range(0, length, piece):
                crc = zlib.crc32(bytes(min(piece, length - done)), crc)
        elif kind == CRC:
            if struct.unpack('<I', _read(stream, 4))[0] != crc:
                raise SparseError('The image fails its checksum.')
        else:
            raise SparseError(f'Unknown chunk type {kind:#x}.')
        offset += length
    if offset != total:
        raise SparseError('The image is shorter than it says.')


def written_bytes(stream):
    """How many bytes runs() would write, without reading the data."""
    block, _total, chunks, csize = header(stream)
    n = 0
    for _ in range(chunks):
        kind, _r, count, size = struct.unpack('<HHII', _read(stream, 12))
        _read(stream, csize - 12)
        if kind in (RAW, FILL):
            n += count * block
        skip = size - csize
        if skip:
            stream.seek(skip, 1)
    return n
