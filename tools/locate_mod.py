"""Resolve which vanilla pak each file in a loose-file mod zip belongs to.

Takes one or more mod zips (the loose-file format: partial EXMLs in a folder
tree), derives the candidate game paths for each file, and looks them up in
the pak index built by index_paks.py. Lookup is a pure md5-hash check, so a
whole mod resolves in microseconds once the index is loaded.

Candidate forms tried per mod file (first hit wins):
  1. as-shipped path (e.g. GLOBALS/gcskyglobals.globals.EXML)
  2. extension swapped to the game format (.EXML/.MXML -> .MBIN), plus
     upper/lower variants of both full path and bare filename
  3. bare filename variants (some paks store no directory structure)

Usage:
    python -m tools.locate_mod "Personal - 72 Minute Days.zip" [more.zips...]

Exit code 1 if any file could not be resolved (new files are expected to
resolve - they become new pak entries - but path-form misses deserve a look).
"""
import argparse
import glob as globmod
import hashlib
import json
import os
import sys
import zipfile

from . import index_paks, repo

DEFAULT_INDEX = str(repo.index_file())

# Loose-mod source format -> in-pak game format.
EXT_SWAP = {".exml": ".mbin", ".mxml": ".mbin"}


def candidate_forms(name: str) -> list:
    """Generate lookup candidates from a path relative to the mod root."""
    n = name.replace("\\", "/").strip("/")
    base = n.rsplit("/", 1)[-1]
    cands = [n]

    def add(c):
        if c and c not in cands:
            cands.append(c)

    ext = os.path.splitext(base)[1]
    if ext.lower() in EXT_SWAP:
        swapped_full = n[: -len(ext)] + EXT_SWAP[ext.lower()]
        swapped_base = base[: -len(ext)] + EXT_SWAP[ext.lower()]
        for s in (swapped_full, swapped_base):
            add(s)
            add(s.upper())
            add(s.lower())
    for s in (n, base):
        add(s.upper())
        add(s.lower())
    # The game's loose loader implicitly maps mod files into METADATA\ when the
    # shipped path doesn't already carry a game-root prefix - some mod authors
    # omit it. Try the METADATA-prefixed forms last (both prefix cases).
    if not n.lower().startswith(("metadata/", "models/", "textures/", "sound/")):
        for c in list(cands):
            add("METADATA/" + c)
            add("metadata/" + c)
    return cands


def resolve(name: str, hashmap: dict):
    """Return (matched_candidate, (pak, stored_path)) or (None, None)."""
    for cand in candidate_forms(name):
        hit = hashmap.get(hashlib.md5(cand.encode()).digest())
        if hit:
            return cand, hit
    return None, None


def describe_match(name: str, cand: str) -> str:
    base = name.replace("\\", "/").strip("/").rsplit("/", 1)[-1]
    if cand == name:
        return "exact path"
    if cand == base:
        return "bare-name match"
    if cand.lower() == name.lower():
        return "case-insensitive match"
    return f"transformed match ({name} -> {cand})"


# Files commonly shipped in loose mod zips that are not game content.
SKIP_EXT = {".txt", ".md", ".bak", ".json", ".png", ".jpg", ".lua"}


def game_rel_path(name: str) -> str:
    """Strip the zip's top-level mod-folder component ('Mod Name/GLOBALS/x' -> 'GLOBALS/x')."""
    n = name.replace("\\", "/").strip("/")
    parts = n.split("/", 1)
    return parts[1] if len(parts) == 2 else n


def mod_entries(path: str) -> list[str]:
    """All file entries of a mod ('/'-separated): zip members or folder files."""
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                full = os.path.join(root, f)
                out.append(os.path.relpath(full, path).replace("\\", "/"))
        return sorted(out)
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if not n.endswith("/")]


def locate_zip(zip_path: str, hashmap: dict):
    """Resolve every game file in a mod (zip or loose folder).

    Returns (unresolved_count, resolved_paths); the caller aggregates
    across mods."""
    unresolved = 0
    resolved_paths = []
    print(f"\n=== {os.path.basename(zip_path)} ===")
    for name in mod_entries(zip_path):
        if name.endswith("/"):
            continue  # folder entry
        base = name.rsplit("/", 1)[-1]
        if base.lower() == "mod_info.json":
            continue  # loader metadata, not a game file
        if os.path.splitext(base)[1].lower() in SKIP_EXT:
            print(f"  {name}")
            print(f"      (skipped: not a game file)")
            continue
        # Zip entries sit under a wrapper folder named after the mod; loose
        # folder entries are already relative to the mod root.
        rel = game_rel_path(name) if not os.path.isdir(zip_path) else name
        cand, hit = resolve(rel, hashmap)
        if hit:
            pak, stored = hit
            print(f"  {name}")
            print(f"      -> {pak} : {stored}   [{describe_match(rel, cand)}]")
            resolved_paths.append((rel, cand, pak, stored))
        else:
            unresolved += 1
            print(f"  {name}")
            print(f"      -> NOT FOUND (would be a new pak entry)")
    paks = sorted({p for _, _, p, _ in resolved_paths})
    print(f"  summary: paks touched: " + (", ".join(paks) if paks else "none"))
    return unresolved, resolved_paths


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("zips", nargs="+", help="loose-file mod zips")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    args = ap.parse_args()

    index = index_paks.load_index(args.index)
    stale = index_paks.is_stale(index)
    print(f"index: {index['total_paks']} paks, {index['total_files']} files, "
          f"game version {index.get('game_version') or 'unknown'}, "
          f"generated {index['generated']}"
          + ("  [STALE - game paks changed; re-run index_paks.py]" if stale else ""))
    hashmap = index_paks.build_hashmap(index)

    zips = []
    for pattern in args.zips:
        matches = globmod.glob(pattern)
        zips.extend(matches if matches else [pattern])

    unresolved = 0
    owner = {}  # resolved game path -> [zip names that assign it]
    for z in zips:
        n, resolved = locate_zip(z, hashmap)
        unresolved += n
        for rel, cand, pak, stored in resolved:
            owner.setdefault(stored, []).append(os.path.basename(z))

    # Cross-mod collisions: 2+ mods assign the same pak path -> merge mandatory.
    collisions = {p: zips_ for p, zips_ in owner.items() if len(zips_) > 1}
    if collisions:
        print(f"\n!!! COLLIDING MODS - one MBIN per pak path wins, merging is mandatory:")
        for p, zips_ in sorted(collisions.items()):
            print(f"  {p}")
            for zn in zips_:
                print(f"      {zn}")
    if unresolved:
        print(f"\n{unresolved} file(s) unresolved - check path forms or treat as new files")
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
