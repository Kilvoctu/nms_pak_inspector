"""Uniform mod-archive access: zip (ZipFile), 7z (py7zr), rar (7-Zip binary); names/read/info facade."""

import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

from . import repo

ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")
_7Z_PATHS = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
)


def is_archive(path) -> bool:
    return str(path).lower().endswith(ARCHIVE_SUFFIXES)


def find_7z() -> str | None:
    """Path to 7z.exe (PATH, then the standard install dirs), None if absent."""
    exe = shutil.which("7z") or shutil.which("7za")
    if exe:
        return exe
    for cand in _7Z_PATHS:
        if Path(cand).exists():
            return cand
    return None


def open_archive(path) -> "ZipArchive | SevenZipArchive | RarArchive":
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return ZipArchive(path)
    if suffix == ".7z":
        return SevenZipArchive(path)
    if suffix == ".rar":
        return RarArchive(path)
    raise ValueError(f"not a supported archive: {path}")


def rewrite_entry(path, entry: str, data: bytes) -> None:
    """Replace one member, keeping the archive format (rar is read-only)."""
    path = Path(path)
    if path.suffix.lower() == ".rar":
        raise ValueError(
            "RAR archives cannot be rewritten - save this mod as .zip or .7z"
        )
    arc = open_archive(path)
    names = arc.names()
    tmp = path.with_suffix(path.suffix + ".editing")
    write_archive(tmp, {n: (data if n == entry else arc.read(n)) for n in names},
                  fmt=path.suffix.lower())
    os.replace(tmp, path)


def write_archive(path, entries: dict, fmt: str = ".zip") -> None:
    """Write a new archive (name -> bytes) in the given format."""
    if fmt == ".zip":
        with ZipFile(path, "w") as zout:
            for name, data in entries.items():
                zout.writestr(name, data)
        return
    if fmt == ".7z":
        import py7zr

        with py7zr.SevenZipFile(path, "w") as z:
            for name, data in entries.items():
                z.writestr(data, name)
        return
    raise ValueError(f"cannot write {fmt} archives")


class ZipArchive:
    """ZipFile backend (the fast path everything else mirrors)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def names(self) -> list[str]:
        with ZipFile(self._path) as z:
            return [n for n in z.namelist() if not n.endswith("/")]

    def read(self, name: str) -> bytes:
        with ZipFile(self._path) as z:
            return z.read(name)

    def info(self, name: str) -> tuple[int, int]:
        with ZipFile(self._path) as z:
            i = z.getinfo(name)
        return i.file_size, i.CRC


class SevenZipArchive:
    """py7zr backend; CRCs come from the 7z headers (no data reads)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _infos(self) -> dict:
        import py7zr

        out = {}
        with py7zr.SevenZipFile(self._path) as z:
            for f in z.list():
                if not f.is_directory:
                    out[f.filename] = (f.uncompressed, f.crc32 or 0)
        return out

    def names(self) -> list[str]:
        return list(self._infos())

    def read(self, name: str) -> bytes:
        import tempfile

        import py7zr

        with tempfile.TemporaryDirectory() as td:
            with py7zr.SevenZipFile(self._path) as z:
                z.extract(path=td, targets=[name])
            return (Path(td) / name).read_bytes()

    def info(self, name: str) -> tuple[int, int]:
        infos = self._infos()
        if name not in infos:
            raise KeyError(name)
        return infos[name]


class RarArchive:
    """RAR via the 7-Zip binary (RAR decoding is proprietary)."""

    def __init__(self, path: Path) -> None:
        self._path = str(path)
        self._exe = find_7z()
        if not self._exe:
            raise RuntimeError(
                "7z.exe is required to read RAR mod archives - install 7-Zip"
            )

    def _listing(self) -> list[dict]:
        result = subprocess.run(  # noqa: PLW1510 - failures reported via stderr
            [self._exe, "l", "-slt", "-y", self._path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=repo._SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            raise RuntimeError(f"7z listing failed: {result.stderr.strip()[:200]}")
        entries: list[dict] = []
        block: dict = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                # Real member blocks carry "Folder = "; the archive's own
                # header block does not - skip it.
                if "dir" in block:
                    entries.append(block)
                block = {}
                continue
            if line.startswith("Path = "):
                block["path"] = line[7:].replace("\\", "/")
            elif line.startswith("Size = "):
                block["size"] = int(line[7:] or 0)
            elif line.startswith("CRC = "):
                crc = line[6:].strip()
                block["crc"] = int(crc, 16) if crc and crc != "00000000" else 0
            elif line.startswith("Folder = "):
                block["dir"] = line[9:].strip() == "+"
        if "dir" in block:
            entries.append(block)
        return entries

    def names(self) -> list[str]:
        return [e["path"] for e in self._listing() if not e.get("dir")]

    def read(self, name: str) -> bytes:
        result = subprocess.run(  # noqa: PLW1510 - failures reported via stderr
            [self._exe, "e", "-so", "-y", "--", self._path, name],
            capture_output=True, timeout=300,
            creationflags=repo._SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:200]
            raise RuntimeError(f"7z extraction failed for {name}: {detail}")
        return result.stdout

    def info(self, name: str) -> tuple[int, int]:
        for e in self._listing():
            if e["path"] == name:
                return e.get("size", 0), e.get("crc", 0)
        raise KeyError(name)
