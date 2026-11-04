"""Minimal HGPAK v2 (Windows) pak reader and targeted injector (full format spec in AGENTS.md)."""

import bisect
import hashlib
import struct
from pathlib import Path

import zstandard as zstd

MAGIC = b"HGPAK"
CHUNK_SIZE = 0x10000  # decompressed bytes per chunk (windows/PC paks)
ENTRY_FMT = "<16sQQ"
ENTRY_SIZE = 0x20
BULK_LEVEL = 7  # zstd level for re-chunked windows with no changed bytes:
# ~3x faster than the quality level at ~2% larger pak size


def _bins(n: int, b: int = 0x10) -> int:
    return (n + b - 1) // b


def _req_chunk_bytes(chunk_size: int) -> int:
    """Storage bytes for a chunk: its compressed size padded to 0x10."""
    return 0x10 * _bins(chunk_size)


def _roundup16(x: int) -> int:
    return (x >> 4 << 4) + ((x & 0xF) and 0x10)


def _padding16(x: int) -> int:
    return (0x10 - (x & 0xF)) & 0xF


class PakError(Exception):
    pass


class Pak:
    """Reader for one HGPAK v2 file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._f = open(self.path, "rb")
        self._chunk_cache: dict[int, bytes] = {}
        self._read()

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- reading ----------------------------------------------------------

    def _read(self) -> None:
        f = self._f
        f.seek(0)
        if f.read(5) != MAGIC:
            raise PakError(f"{self.path} is not an HGPAK file")
        f.seek(8)
        self.version, self.file_count, self.chunk_count, self.is_compressed, self.data_offset = (
            struct.unpack("<QQQ?7xQ", f.read(0x28))
        )
        if self.version != 2:
            raise PakError(f"unsupported HGPAK version {self.version}")
        f.seek(0x30)
        self.name_hash = f.read(16)
        f.seek(0x40)
        _, filepath_len = struct.unpack("<QQ", f.read(16))

        # Real file entries live at 0x50; the "pseudo-entry" is the header
        # itself (name hash @0x30, data_offset @0x40, filepath_len @0x48),
        # which is why file_count = real_files + 1.
        n_real = self.file_count - 1
        from io import SEEK_SET

        f.seek(0x50, SEEK_SET)
        self.entries = []
        for _ in range(n_real):
            h, off, size = struct.unpack(ENTRY_FMT, f.read(ENTRY_SIZE))
            self.entries.append([h, off, size])
        self.chunk_sizes = []
        if self.is_compressed:
            raw = f.read(8 * self.chunk_count)
            self.chunk_sizes = list(struct.unpack(f"<{self.chunk_count}Q", raw))
        else:
            total = self.entries[-1][1] + _roundup16(self.entries[-1][2]) - self.data_offset
            count = _bins(total, CHUNK_SIZE)
            self.chunk_sizes = [CHUNK_SIZE] * (count - 1) + [total - CHUNK_SIZE * (count - 1)]
        # Chunk offsets are derived: each chunk's storage is 0x10-aligned.
        self.chunk_offsets = []
        off = self.data_offset
        for sz in self.chunk_sizes:
            self.chunk_offsets.append(off)
            off += _req_chunk_bytes(sz)

        # The filepath-data blob sits at data_offset.
        self.filepath_data = self._read_range(self.data_offset, filepath_len)
        self.filenames = [
            x.decode() for x in self.filepath_data.rstrip(b"\r\n").split(b"\r\n")
        ]
        if len(self.filenames) != n_real:
            raise PakError(
                f"filepath data lists {len(self.filenames)} names, "
                f"table has {n_real} entries"
            )
        self.file_map = {}
        for i, name in enumerate(self.filenames):
            entry = self.entries[i]
            if hashlib.md5(name.encode()).digest() != entry[0]:
                raise PakError(f"entry hash mismatch for {name!r}")
            self.file_map[name] = entry

    def _decompress_chunk(self, idx: int) -> bytes:
        if idx in self._chunk_cache:
            return self._chunk_cache[idx]
        self._f.seek(self.chunk_offsets[idx])
        raw = self._f.read(self.chunk_sizes[idx])
        if not self.is_compressed:
            data = raw
        else:
            try:
                data = zstd.ZstdDecompressor().decompress(raw, max_output_size=CHUNK_SIZE)
            except zstd.ZstdError:
                if len(raw) == CHUNK_SIZE:
                    data = raw  # chunk was stored uncompressed
                else:
                    raise
        # Small sequential cache: window reads sweep the stream in order, so
        # a modest FIFO absorbs the overlap between neighboring windows
        # (files spanning a boundary are read once per window).
        if len(self._chunk_cache) >= 32:
            self._chunk_cache.clear()
        self._chunk_cache[idx] = data
        return data

    def _read_range(self, abs_offset: int, size: int) -> bytes:
        """Read `size` bytes of the decompressed stream starting at abs_offset."""
        rel = abs_offset - self.data_offset
        first, last = rel // CHUNK_SIZE, (rel + size - 1) // CHUNK_SIZE
        out = bytearray()
        for idx in range(first, last + 1):
            out += self._decompress_chunk(idx)
        start = rel - first * CHUNK_SIZE
        return bytes(out[start:start + size])

    # --- public API ---------------------------------------------------------

    def file_names(self) -> list[str]:
        return list(self.filenames)

    def read_file(self, name: str) -> bytes:
        entry = self.file_map.get(name)
        if entry is None:
            raise KeyError(f"{name!r} not in pak")
        return self._read_range(entry[1], entry[2])

    def extract_file(self, name: str, out_dir: str | Path) -> Path:
        out = Path(out_dir) / Path(name.replace("/", "\\"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.read_file(name))
        return out


def build_filepath_data(names: list[str]) -> bytes:
    return b"".join(n.encode() + b"\r\n" for n in names)


def inject(
    source: str | Path,
    changes: dict[str, bytes],
    out: str | Path,
    *,
    compress: bool = True,
    level: int = 12,
    bulk_level: int = BULK_LEVEL,
    full_check: bool = False,
) -> dict:
    """Rewrite `source` pak with `changes` applied into `out` (no new files: full re-chunk).

    Untouched windows copy compressed-verbatim (layout unchanged); a shifted tail re-chunks at ``bulk_level``."""
    src = Pak(source)
    names = list(src.filenames)
    added = [n for n in changes if n not in src.file_map]
    names.extend(added)

    fp_new = build_filepath_data(names)
    # Ordered file list of the new data stream: filepath blob, then files.
    files: list[tuple[str, bytes | None, int]] = []  # (name, new_bytes or None, orig_size)
    for n in names:
        if n in changes:
            files.append((n, changes[n], len(changes[n])))
        else:
            files.append((n, None, src.file_map[n][2]))

    fp_same = fp_new == src.filepath_data
    # Same-layout fast path: identical filepath blob + unchanged padded slots,
    # so all stream positions hold and untouched windows equal source windows.
    layout_identical = (
        fp_same
        and not added
        and all(
            _roundup16(len(b)) == _roundup16(src.file_map[n][2])
            for n, b in changes.items()
        )
    )

    # Absolute padded-stream ranges (new coordinates) whose bytes change.
    changed_ranges: list[tuple[int, int]] = []
    if fp_same:
        cursor = _roundup16(len(fp_new))
        for name, new_bytes, size in files:
            if new_bytes is not None:
                changed_ranges.append((cursor, cursor + _roundup16(size)))
            cursor += _roundup16(size)
    else:
        changed_ranges = [(0, _roundup16(len(fp_new)))]  # blob differs: leading region changes

    first_change = min((a for a, _ in changed_ranges), default=None)
    total_padded = _roundup16(len(fp_new)) + sum(_roundup16(s) for _, _, s in files)
    chunk_count = _bins(total_padded, CHUNK_SIZE)

    # Windows [0, boundary) lie entirely before the first changed byte and can
    # be copied verbatim from the source regardless of layout shifts.
    boundary = (first_change // CHUNK_SIZE) if first_change is not None else 0
    boundary = min(boundary, src.chunk_count)
    verbatim = src.is_compressed and compress
    if not verbatim:
        boundary = 0

    # Header sizing.
    n_real = len(files)
    file_count = n_real + 1  # header convention: real files + filepath blob
    data_offset = 0x50 + 0x20 * n_real + (8 * chunk_count if compress else 0)
    data_offset += _padding16(data_offset)

    # Stream reader over the new content.
    stream = _NewStream(src, files, fp_new)

    copied_chunks = 0
    copied_bytes = 0
    compressor = zstd.ZstdCompressor(level=level)
    bulk_compressor = (
        zstd.ZstdCompressor(level=bulk_level) if bulk_level != level else compressor
    )
    with open(out, "wb") as fo:
        fo.write(struct.pack("<5s3xQQQ?7xQ", MAGIC, 2, file_count, chunk_count, compress, data_offset))
        fo.seek(0x30)
        fo.write(src.name_hash)
        fo.write(struct.pack("<QQ", data_offset, len(fp_new)))
        # Real file entries at 0x50 (the pseudo-entry is the header itself).
        cursor = _roundup16(len(fp_new))
        for name, new_bytes, size in files:
            entry_hash = hashlib.md5(name.encode()).digest()
            fo.write(struct.pack(ENTRY_FMT, entry_hash, data_offset + cursor, size))
            cursor += _roundup16(size)
        # Reserve chunk index.
        chunk_index_offset = fo.tell()
        if compress:
            fo.write(b"\x00" * (8 * chunk_count))
        pad = _padding16(fo.tell())
        fo.write(b"\x00" * pad)
        actual_data_offset = fo.tell()
        if actual_data_offset != data_offset:
            raise PakError(f"data_offset miscalculation: {data_offset} != {actual_data_offset}")

        # Data region.
        written = 0
        sizes_written: list[int] = []
        for k in range(chunk_count):
            a = k * CHUNK_SIZE
            b = min(a + CHUNK_SIZE, total_padded)
            overlaps = _overlaps(a, b, changed_ranges)
            if (
                verbatim
                and k < src.chunk_count
                and (k < boundary or (layout_identical and not overlaps))
            ):
                # Copy the source chunk's stored bytes verbatim (0x10-aligned).
                stored = _req_chunk_bytes(src.chunk_sizes[k])
                src._f.seek(src.chunk_offsets[k])
                data = src._f.read(stored)
                size = src.chunk_sizes[k]
                copied_chunks += 1
                copied_bytes += stored
            else:
                window = stream.read(a, b - a)
                if compress:
                    comp = compressor if overlaps else bulk_compressor
                    data = comp.compress(window)
                else:
                    data = window
                size = len(data)
            sizes_written.append(size)
            fo.write(data)
            fo.write(b"\x00" * (_req_chunk_bytes(size) - len(data)))
            written += 1
        assert written == chunk_count, "chunk count mismatch"

        # Fill in the chunk index (compressed sizes).
        if compress:
            fo.seek(chunk_index_offset)
            for size in sizes_written:
                fo.write(struct.pack("<Q", size))

    # Self-check: changed/added files read back byte-exactly; unchanged files
    # are verbatim (or stream) copies.
    check = Pak(out)
    try:
        if check.filenames != names:
            raise PakError("output pak filename list mismatch")
        for name, new_bytes, _ in files:
            if new_bytes is None and not full_check:
                continue
            expect = new_bytes if new_bytes is not None else src.read_file(name)
            if check.read_file(name) != expect:
                raise PakError(f"output verification failed for {name!r}")
    finally:
        check.close()
    src.close()

    return {
        "files": len(files),
        "chunks": chunk_count,
        "chunks_copied": copied_chunks,
        "chunks_recompressed": chunk_count - copied_chunks,
        "bytes_copied": copied_bytes,
        "size_before": Path(source).stat().st_size,
        "size_after": Path(out).stat().st_size,
        "added_files": added,
    }


def _overlaps(a: int, b: int, ranges: list[tuple[int, int]]) -> bool:
    """Whether window [a, b) intersects any (start, end) range. Ranges are
    sorted ascending; a linear scan with early exit is plenty (few ranges)."""
    for start, end in ranges:
        if start >= b:
            break
        if end > a:
            return True
    return False


class _NewStream:
    """Lazy reader over the new data stream (filepath blob + files + padding)."""

    def __init__(self, src: Pak, files, fp_bytes: bytes):
        self.src = src
        self._parts: list[tuple[int, int, bytes | None, str | None]] = []
        # (offset, length, mem_bytes_or_None, src_name_or_None)
        self._parts.append((0, len(fp_bytes), fp_bytes, None))
        offset = _roundup16(len(fp_bytes))
        for name, new_bytes, size in files:
            if new_bytes is not None:
                self._parts.append((offset, size, new_bytes, None))
            else:
                self._parts.append((offset, size, None, name))
            offset += _roundup16(size)
        self.total = offset
        self._starts = [p[0] for p in self._parts]

    def read(self, start: int, length: int) -> bytes:
        out = bytearray()
        end = start + length
        # Parts are contiguous and sorted; bisect to the covering part.
        i = bisect.bisect_right(self._starts, start) - 1
        i = max(i, 0)
        parts = self._parts
        while i < len(parts) and parts[i][0] < end:
            off, plen, mem, src_name = parts[i]
            i += 1
            part_padded_end = off + _roundup16(plen)
            if part_padded_end <= start:
                continue
            if mem is None:
                mem = self.src.read_file(src_name)
            lo = max(start, off)
            hi = min(end, off + plen)
            if lo < hi:
                out += mem[lo - off:hi - off]
            pad_lo = max(start, off + plen)
            pad_hi = min(end, part_padded_end)
            if pad_lo < pad_hi:
                out += b"\x00" * (pad_hi - pad_lo)
        if len(out) < length:
            out += b"\x00" * (length - len(out))
        return bytes(out[:length])
