"""Build a static path->pak index of every .pak in the game's PCBANKS folder.

Reads only pak headers + filepath data (never the bulk data), so the whole
scan of ~100 paks / 33 GB takes ~3 seconds. The resulting pak_index.json is
a static asset: refresh it only after a game update.

Usage:
    python -m tools.index_paks [--pcbanks PATH] [--out PATH]

The output maps every pak to its stored file paths (exactly as the pak
records them - bare names for some paks, full paths for others), plus
sizes and a game-version stamp for cache invalidation.
"""
import argparse
import glob
import hashlib
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone

from . import mbincompiler, paklib, repo

DEFAULT_OUT = str(repo.index_file())


def _peek_header(pak_path: str) -> int:
    """Cheap sanity read (magic + version) - no decompression."""
    with open(pak_path, "rb") as f:
        if f.read(5) != paklib.MAGIC:
            raise ValueError(f"{pak_path} is not an HGPAK file")
        f.seek(8)
        version, file_count = struct.unpack("<QQ", f.read(16))
        if version != 2:
            raise ValueError(f"{pak_path}: unsupported HGPAK version {version}")
        return file_count


def get_game_version(pcbanks: str):
    """Best-effort NMS version for cache stamping (MS Store package version)."""
    return mbincompiler.game_version()


def build_index(pcbanks: str, verbose: bool = True) -> dict:
    t_start = time.perf_counter()

    pak_paths = sorted(glob.glob(os.path.join(pcbanks, "*.pak")))
    if verbose:
        print(f"{len(pak_paths)} pak files in {pcbanks}")

    # Pass 1: header-only sanity scan (magic/version peek, no decompression).
    t0 = time.perf_counter()
    headers = []
    for p in pak_paths:
        _peek_header(p)
        headers.append(p)
    t_header = time.perf_counter() - t0
    total_bytes = sum(os.stat(p).st_size for p in headers)

    # Pass 2: full index - decompress filepath data, verify entry hashes.
    t0 = time.perf_counter()
    paks = {}
    hash_mismatches = 0
    total_files = 0
    for p in headers:
        pk = paklib.Pak(p)  # raises when the entry table is inconsistent
        paths = list(pk.filenames)
        # Pak already validates the table; recount for the stats anyway.
        for i, name in enumerate(paths):
            if pk.entries[i][0] != hashlib.md5(name.encode()).digest():
                hash_mismatches += 1
        paks[os.path.basename(p)] = {
            "size": os.stat(p).st_size,
            "mtime": int(os.stat(p).st_mtime),
            "files": paths,
        }
        total_files += len(paths)
        pk.close()
    t_index = time.perf_counter() - t0

    if verbose:
        print(f"header-only scan: {t_header * 1000:.0f} ms ({total_bytes / 1e9:.2f} GB)")
        print(f"full index: {t_index:.2f} s, {total_files} file paths, "
              f"{hash_mismatches} hash mismatches")
        print(f"total build time: {time.perf_counter() - t_start:.2f} s")

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pcbanks": os.path.abspath(pcbanks),
        "game_version": get_game_version(pcbanks),
        "total_paks": len(paks),
        "total_files": total_files,
        "hash_mismatches": hash_mismatches,
        "index_seconds": round(t_index, 2),
        "paks": paks,
    }


def is_stale(index: dict, pcbanks: str = None) -> bool:
    """True if any pak on disk changed size or mtime since the index was built.

    Game updates restore vanilla paks (Store blockmap), changing mtimes -
    this is the refresh trigger. Costs one stat call per pak (~milliseconds).
    """
    pcbanks = pcbanks or index["pcbanks"]
    for pak, info in index["paks"].items():
        p = os.path.join(pcbanks, pak)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return True
        if st.st_size != info["size"] or int(st.st_mtime) != info.get("mtime"):
            return True
    return False


def load_index(path: str = DEFAULT_OUT) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_hashmap(index: dict) -> dict:
    """md5(path-as-stored) -> (pak, path). Verify-only assumption: the index
    was built with 0 hash mismatches."""
    hm = {}
    for pak, info in index["paks"].items():
        for p in info["files"]:
            hm.setdefault(hashlib.md5(p.encode()).digest(), (pak, p))
    return hm


def _default_pcbanks() -> str | None:
    """CLI default: the PCBANKS resolved from the state file's game folder."""
    import configparser

    state = repo.state_file()
    if not state.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(state, encoding="utf-8")
    game = cp.get("General", "game_folder", fallback=None)
    if game:
        return repo.find_pcbanks(game.strip().strip('"'))
    pcbanks = cp.get("General", "pcbanks_folder", fallback=None)
    return pcbanks.strip().strip('"') if pcbanks else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcbanks", default=None,
                    help="PCBANKS path (default: from state.ini's game folder)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    pcbanks = args.pcbanks or _default_pcbanks()
    if not pcbanks:
        print("No PCBANKS known: pass --pcbanks or set the game folder in the app")
        return 1
    index = build_index(pcbanks)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    print(f"wrote {args.out} ({os.stat(args.out).st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
