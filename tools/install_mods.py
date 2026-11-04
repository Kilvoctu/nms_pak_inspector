"""Managed loose install: one GAMEDATA\\MODS\\nmspi-managed folder, a pure function of loose_state.json."""

import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

from . import mbincompiler, merge_mods, repo
from .inject_mods import _vanilla_root
from .merge_mods import MergeError
from .verify_mods import _extract_and_decompile, _parse_mod_tree, _read_mod_data

MANAGED_FOLDER = "nmspi-managed"
STATE_FILE = repo.project_root() / "loose_state.json"
WORK_ROOT = repo.cache_dir() / "_loosework"
BUILD_CACHE = repo.cache_dir() / "loose_built.json"
BUILD_VERSION = 1  # bump to force a full regenerate after pipeline changes
TREE_SUFFIXES = (".mbin", ".exml", ".mxml")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    if state:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        STATE_FILE.unlink(missing_ok=True)  # keep the project root clean


def _load_build_cache() -> dict:
    if BUILD_CACHE.exists():
        try:
            return json.loads(BUILD_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _save_build_cache(cache: dict) -> None:
    if cache:
        BUILD_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    else:
        BUILD_CACHE.unlink(missing_ok=True)


def _source_stamps(mods: dict) -> dict:
    """Cheap per-entry [size, mtime] stamps for every mod source (one zip pass each)."""
    stamps: dict[str, dict] = {}
    for name, mod in mods.items():
        path = Path(mod.path)
        entry_stamps: dict = {}
        if path.is_dir():
            for hit in mod.game_files:
                try:
                    st = (path / hit.entry).stat()
                    entry_stamps[hit.entry] = [st.st_size, int(st.st_mtime)]
                except OSError:
                    entry_stamps[hit.entry] = None
        else:
            try:
                with ZipFile(path) as z:
                    infos = {i.filename: i for i in z.infolist()}
                for hit in mod.game_files:
                    info = infos.get(hit.entry)
                    entry_stamps[hit.entry] = (
                        [info.file_size, list(info.date_time)] if info else None
                    )
            except (OSError, RuntimeError):
                entry_stamps = {h.entry: None for h in mod.game_files}
        stamps[name] = entry_stamps
    return stamps


def _target_fingerprint(parts: list, stamps: dict, index: dict) -> dict:
    """Identity of one target's content: contributor list + source stamps + index build."""
    contribs = [
        [order, name, hit.entry, stamps.get(name, {}).get(hit.entry)]
        for order, name, hit, _rel, _is_tree in parts
    ]
    return {"v": BUILD_VERSION, "index": index.get("generated"), "contribs": contribs}


def managed_dir(pcbanks: str) -> Path:
    return Path(pcbanks).parent / "MODS" / MANAGED_FOLDER


def disablemods_flag() -> Path | None:
    """The game's global 'all loose mods disabled' flag, if locatable."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "HelloGames" / "NMS" / "DefaultUser" / "cache" / "DISABLEMODS"


def loose_mods_disabled() -> bool:
    flag = disablemods_flag()
    return bool(flag and flag.exists())


def installed_names(pcbanks: str) -> set[str]:
    """Mods currently loose-installed via the managed folder (manifest-derived)."""
    return set(_load_state())


def remove_all(pcbanks: str, log) -> dict:
    """Wipe the managed folder and forget the manifest."""
    _save_state({})
    _save_build_cache({})
    managed = managed_dir(pcbanks)
    if managed.exists():
        shutil.rmtree(managed)
    log(f"Removed all loose installs ({MANAGED_FOLDER})")
    return {"removed": True}


def install_mod(mod, scan: dict, index: dict, pcbanks: str, log) -> dict:
    """Add one mod to the manifest and rebuild the managed folder."""
    if loose_mods_disabled():
        log(
            "WARNING: the game's DISABLEMODS flag is set "
            "(%APPDATA%\\HelloGames\\NMS\\...\\cache\\DISABLEMODS) - "
            "loose mods will not load until it is removed"
        )
    state = _load_state()
    order = max((e.get("order", 0) for e in state.values()), default=0) + 1
    paths = [_loose_path(hit)[0] for hit in sorted(mod.game_files, key=lambda h: h.entry)]
    state[mod.name] = {"order": order, "files": paths}
    _save_state(state)
    result = rebuild(scan, index, pcbanks, log)
    log(f"Installed {mod.name}: {len(paths)} file(s) -> {MANAGED_FOLDER}")
    rootless = [p for p in paths if "/" not in p]
    if rootless:
        # No vanilla file lives at GAMEDATA root, so these targets never load.
        log(
            f"WARNING: {len(rootless)} file(s) ship without a directory prefix and "
            "won't load loose (likely a mod packaging issue):"
        )
        for p in rootless[:8]:
            log(f"   {p}")
        if len(rootless) > 8:
            log(f"   … +{len(rootless) - 8} more")
    return {"installed": len(paths), "rebuilt": result["files"], "dest": str(managed_dir(pcbanks))}


def uninstall_mod(mod_name: str, scan: dict, index: dict, pcbanks: str, log) -> dict:
    """Remove one mod from the manifest and rebuild the managed folder."""
    state = _load_state()
    if mod_name not in state:
        log(f"{mod_name} had no loose install")
        return {"removed": False}
    del state[mod_name]
    _save_state(state)
    result = rebuild(scan, index, pcbanks, log)
    log(f"Removed loose install of {mod_name}; {result['files']} managed file(s) remain")
    return {"removed": True}


def _loose_path(hit) -> tuple[str, bool]:
    """Loose target path (GAMEDATA-relative, '/'-separated) + whether content is a tree.
    Always built from the mod's own rel path (stored names can be bare); EXML/MXML -> .MBIN."""
    rel = hit.rel.replace("\\", "/")
    is_tree = rel.lower().endswith(TREE_SUFFIXES)
    if rel.lower().endswith((".exml", ".mxml")):
        rel = rel[:-5] + ".MBIN"
    return rel, is_tree


def rebuild(scan: dict, index: dict, pcbanks: str, log) -> dict:
    """Regenerate the managed folder from the manifest (built aside, then swapped in).
    Unchanged targets are copied over; changed ones generate in parallel; shared paths merge."""
    started = time.monotonic()
    state = _load_state()
    mods = {m.name: m for m in scan["mods"]}
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    compiler = mbincompiler.ensure_compiler()

    groups: dict[str, list] = {}
    for name, entry in state.items():
        mod = mods.get(name)
        if mod is None:  # mod source gone (deleted zip): skip, keep manifest entry
            log(f"WARNING: {name} is loose-installed but its source is gone - skipped")
            continue
        for hit in sorted(mod.game_files, key=lambda h: h.entry):
            rel, is_tree = _loose_path(hit)
            key = rel.lower()
            groups.setdefault(key, []).append((entry.get("order", 0), name, hit, rel, is_tree))

    stamps = _source_stamps(mods)
    cache = _load_build_cache()
    managed = managed_dir(pcbanks)
    building = managed.parent / f"{MANAGED_FOLDER}.building"
    if building.exists():
        shutil.rmtree(building)
    building.mkdir(parents=True)

    reuse, generate = [], []
    for key, parts in groups.items():
        parts.sort()  # manifest order = deterministic merge/first-wins order
        fp = _target_fingerprint(parts, stamps, index)
        old = managed / parts[0][3]
        if cache.get(key) == fp and old.is_file():
            reuse.append((key, parts, old))
        else:
            generate.append((key, parts, fp))

    # Warm the vanilla decompile cache serially (its writes are not race-safe).
    for _key, parts, _fp in generate:
        _warm_vanilla(parts, index, pcbanks, log, compiler)

    written = 0
    for _key, parts, old in reuse:
        target = building / parts[0][3]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, target)
        written += 1
    if generate:
        workers = min(8, os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_generate_target, key, parts, mods, index, pcbanks,
                            WORK_ROOT, log, compiler, building)
                for key, parts, _fp in generate
            ]
            for fut in futures:
                fut.result()  # re-raises MergeError etc; building dir is discarded
                written += 1

    if managed.exists():
        shutil.rmtree(managed)
    building.rename(managed)
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    new_cache = {key: cache[key] for key in groups if key in cache}
    for key, _parts, fp in generate:
        new_cache[key] = fp
    _save_build_cache(new_cache)
    log(
        f"loose rebuild: {len(reuse)} file(s) reused, {len(generate)} generated "
        f"in {time.monotonic() - started:.1f}s"
    )
    return {"files": written}


def _warm_vanilla(parts: list, index: dict, pcbanks: str, log, compiler) -> None:
    """Pre-decompile the vanilla base for one target (serial; repeats hit the cache)."""
    if len(parts) == 1:
        _order, _name, hit, _rel, is_tree = parts[0]
        if is_tree and hit.pak:
            _extract_and_decompile(pcbanks, hit.pak, hit.stored, index, log, compiler)
        return
    for _order, _name, hit, _rel, is_tree in parts:
        if hit.pak and is_tree:
            _extract_and_decompile(pcbanks, hit.pak, hit.stored, index, log, compiler)
            return  # only the first pak-backed contributor is the merge base


def _generate_target(key: str, parts: list, mods: dict, index: dict, pcbanks: str,
                     work: Path, log, compiler, building: Path) -> None:
    """Worker: build one target's content and write it into the building folder."""
    data = _content_for_group(key, parts, mods, index, pcbanks, work, log, compiler)
    target = building / parts[0][3]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _content_for_group(key: str, parts: list, mods: dict, index: dict, pcbanks: str,
                       work: Path, log, compiler) -> bytes:
    """Content for one target path: single contributor = per-file content; shared =
    merge all contributor trees onto vanilla and compile to one MBIN."""
    if len(parts) == 1:
        _order, mod_name, hit, rel, is_tree = parts[0]
        return _content_one(mods[mod_name], hit, rel, is_tree, index, pcbanks, work, log, compiler)

    trees = []
    van_base = None
    for _order, mod_name, hit, rel, is_tree in parts:
        mod = mods[mod_name]
        data = _read_mod_data(mod, hit.entry)
        if not is_tree:
            raise MergeError(
                f"{rel} is a raw (non-MBIN) file shipped by several installed mods "
                "with differing bytes - resolve it via pak injection's conflict dialog "
                "or uninstall one of them"
            )
        trees.append(_parse_mod_tree(hit.entry, data, work, log))
        if hit.pak and van_base is None:
            van_base = (hit.pak, hit.stored)
    if van_base is not None:
        van_root = _vanilla_root(
            _extract_and_decompile(pcbanks, van_base[0], van_base[1], index, log, compiler)
        )
    else:
        # No vanilla base: every contributor is a new file; first tree is the base.
        van_root = trees[0]
        trees = trees[1:]
        if not trees:
            return merge_mods.merged_to_mbin(
                van_root, work, hashlib.md5(key.encode()).hexdigest()[:12]
            )
    merged, notes, _conflicts = merge_mods.merge_many(trees, van_root)
    for note in notes:
        log(f"loose merge: {note}")
    stem = hashlib.md5(key.encode()).hexdigest()[:12]
    return merge_mods.merged_to_mbin(merged, work, stem)


def _content_one(mod, hit, rel: str, is_tree: bool, index: dict, pcbanks: str,
                 work: Path, log, compiler) -> bytes:
    """Final loose content for one single-contributor file, mirroring pak injection."""
    data = _read_mod_data(mod, hit.entry)
    if not is_tree:
        return data  # shipped MBIN / texture / other raw: as-is
    tree = _parse_mod_tree(hit.entry, data, work, log)
    stem = hashlib.md5(rel.encode()).hexdigest()[:12]
    if hit.pak:
        van_root = _vanilla_root(
            _extract_and_decompile(pcbanks, hit.pak, hit.stored, index, log, compiler)
        )
        return merge_mods.build_mbin(tree, van_root, work, stem, log)
    return merge_mods.merged_to_mbin(tree, work, stem)  # new file: compile as-is
