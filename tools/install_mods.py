"""Managed loose install: one GAMEDATA\\MODS\\nmspi-managed folder, a pure function of loose_state.json."""

import hashlib
import json
import shutil
from pathlib import Path

from . import mbincompiler, merge_mods, repo
from .inject_mods import _vanilla_root
from .merge_mods import MergeError
from .verify_mods import _extract_and_decompile, _parse_mod_tree, _read_mod_data

MANAGED_FOLDER = "nmspi-managed"
STATE_FILE = repo.project_root() / "loose_state.json"
WORK_ROOT = repo.cache_dir() / "_loosework"
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


def managed_dir(pcbanks: str) -> Path:
    return Path(pcbanks).parent / "MODS" / MANAGED_FOLDER


def disablemods_flag() -> Path | None:
    """The game's global 'all loose mods disabled' flag, if locatable."""
    import os

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
    Shared target paths merge onto vanilla into one MBIN (first-wins); raw-file conflicts raise."""
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

    managed = managed_dir(pcbanks)
    building = managed.parent / f"{MANAGED_FOLDER}.building"
    if building.exists():
        shutil.rmtree(building)
    building.mkdir(parents=True)

    written = 0
    for key, parts in sorted(groups.items()):
        parts.sort()  # manifest order = deterministic merge/first-wins order
        _order, _name, _hit, rel, _is_tree = parts[0]
        target = building / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _content_for_group(key, parts, mods, index, pcbanks, WORK_ROOT, log, compiler)
        target.write_bytes(data)
        written += 1

    if managed.exists():
        shutil.rmtree(managed)
    building.rename(managed)
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    return {"files": written}


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
