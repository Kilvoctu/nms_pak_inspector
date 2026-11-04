"""Background task workers for the NMS Pak Inspector GUI.

``TaskWorker`` runs a plain function on a ``QThread`` so the GUI never blocks;
the function receives a ``log`` callable as its first argument and its return
value is delivered through the ``done(object)`` signal.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from PySide6.QtCore import QThread, Signal

from .. import (
    index_paks,
    inject_mods,
    install_mods,
    locate_mod,
    mbincompiler,
    verify_mods,
)


def friendly_error(exc: BaseException) -> str:
    """Format a worker error message for the console log."""
    return f"{type(exc).__name__}: {exc}"


@dataclass
class FileHit:
    """Resolution result for one file inside a mod zip."""

    entry: str  # path inside the zip, incl. mod-folder prefix
    rel: str  # game-relative path (mod-folder prefix stripped)
    candidate: str | None = None  # candidate form that matched
    pak: str | None = None  # target pak basename
    stored: str | None = None  # path as stored in the pak
    match: str | None = None  # human description of the match
    skipped: bool = False  # not a game file (notes, metadata, ...)


@dataclass
class ModScan:
    """Resolution result for one mod (a zip archive or a loose folder)."""

    name: str  # mod basename (zip stem or folder name)
    path: str  # full path to the zip / folder root
    kind: str = "zip"  # "zip" | "folder"
    hits: list[FileHit] = field(default_factory=list)
    superseded_zips: list[str] = field(default_factory=list)

    @property
    def game_files(self) -> list[FileHit]:
        return [h for h in self.hits if not h.skipped]

    @property
    def paks(self) -> set[str]:
        return {h.pak for h in self.game_files if h.pak}

    @property
    def has_unmatched(self) -> bool:
        return any(h.pak is None for h in self.game_files)


class TaskWorker(QThread):
    """Run ``fn(log, *args)`` in the background and report the outcome.

    Signals are emitted from the worker thread; Qt's queued connections
    deliver them on the GUI thread of the connected receiver.
    """

    log = Signal(str)
    done = Signal(object)
    error = Signal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def run(self) -> None:
        try:
            result = self._fn(self.log.emit, *self._args)
        except Exception as exc:  # noqa: BLE001 - reported to the GUI log
            self.error.emit(friendly_error(exc))
            return
        self.done.emit(result)


def index_worker(log, pcbanks: str) -> dict:
    """Build the path->pak index in the background."""
    log("Building pak index (header-only reads)…")
    index = index_paks.build_index(pcbanks, verbose=False)
    log(
        f"Indexed {index['total_paks']} pak(s), {index['total_files']} file path(s), "
        f"{index['hash_mismatches']} hash mismatch(es) in {index['index_seconds']}s"
    )
    return index


# Top-level folder names that mean "this is already a mod root, don't unwrap".
_GAME_TOP_DIRS = {"GLOBALS", "MODELS", "TEXTURES", "SCRIPTS", "SOUND", "ANIMATIONS"}


def _zip_wrapper_name(zip_path: Path) -> str | None:
    """The single top-level folder inside a zip, if the archive has one.

    Nexus-style downloads are wrapped in a folder named after the mod (e.g.
    ``Diablos_Galaxy_Map/…``) - that inner name is a better display name than
    the download filename. Returns None when entries sit at the zip root, in
    several folders, or under a game-content dir (GLOBALS, MODELS, ...).

    Loose non-game files at the zip root (AMUMSS .lua scripts, readme .txt
    files, ...) are ignored - the scanner skips them anyway."""
    tops: set[str] = set()
    try:
        with ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                n = n.replace("\\", "/")
                if "/" not in n:
                    base = n.rsplit("/", 1)[-1]
                    if os.path.splitext(base)[1].lower() not in locate_mod.SKIP_EXT:
                        return None  # loose GAME file at the zip root
                    continue  # loose non-game file: irrelevant
                top = n.split("/", 1)[0]
                tops.add(top)
                if len(tops) > 1:
                    return None
    except Exception:  # noqa: BLE001 - unreadable zip: keep the stem name
        return None
    if len(tops) != 1:
        return None
    name = next(iter(tops))
    if name.upper() in _GAME_TOP_DIRS:
        return None
    return name


def _mod_sources(
    mods_dir: Path, keep_both: list[list[str]] | None = None
) -> list[tuple[Path, str, str, list[str]]]:
    """Enumerate mods: top-level *.zip files and top-level folders.

    Returns (root, display_name, kind, superseded_zips). A folder that
    contains exactly one subdirectory (an extracted zip's wrapper folder)
    unwraps into it, unless that single child is a game-content folder. A zip
    whose entries all live under one top-level folder is named after that
    inner folder (falling back to the zip filename when that name is taken
    or unusable).

    Two zips sharing an inner folder are versions of one mod: the newest
    (by file mtime) takes the inner name as its identity and the older
    archives are reported as superseded. ``keep_both`` holds zip-filename
    pairs the user chose to keep as separate mods - those skip the supersede
    and the older archive keeps its own (archive-name) identity."""
    sources: list[tuple[Path, str, str, list[str]]] = []
    if not mods_dir.is_dir():
        return sources
    zip_wrappers: dict[Path, str | None] = {}
    for p in sorted(mods_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_file() and p.suffix.lower() == ".zip":
            zip_wrappers[p] = _zip_wrapper_name(p)
        elif p.is_dir():
            root = p
            children = list(p.iterdir())
            if (
                len(children) == 1
                and children[0].is_dir()
                and children[0].name.upper() not in _GAME_TOP_DIRS
            ):
                root = children[0]  # extracted zip: strip the wrapper folder
            sources.append((root, p.name, "folder", []))

    used = {name for _root, name, _kind, _sup in sources if _kind == "folder"}

    def stash(path: Path, display: str, superseded: list[str]) -> None:
        sources.append((path, display, "zip", superseded))

    # Group zips by inner wrapper name (Nexus-style version updates).
    by_inner: dict[str, list[Path]] = {}
    stem_only: list[Path] = []
    for zpath, inner in zip_wrappers.items():
        if inner and inner not in used:
            by_inner.setdefault(inner, []).append(zpath)
        else:
            stem_only.append(zpath)
    for inner, zips in sorted(by_inner.items()):
        if len(zips) == 1:
            stash(zips[0], inner, [])
            used.add(inner)
            continue
        ordered = sorted(zips, key=lambda p: (os.path.getmtime(p), p.name))
        newest = ordered[-1]
        older = ordered[:-1]
        pair = sorted([p.name for p in zips])
        if any(pair == sorted(k) for k in (keep_both or [])):
            # 'Keep both': the newest keeps the inner identity, older
            # archives stay as separate mods under their archive names.
            stash(newest, inner, [])
            used.add(inner)
            for p in older:
                stash(p, p.stem, [])
                used.add(p.stem)
            continue
        older_names = sorted(str(p) for p in older)
        stash(newest, inner, older_names)
        used.add(inner)
        for p in older:
            stash(p, p.stem, [])
            used.add(p.stem)
    for zpath in stem_only:
        display = zpath.stem
        if display in used:
            display = f"{display} ({zpath.stat().st_size})"
        stash(zpath, display, [])
        used.add(display)
    return sources


def stash_replaced_zip(zip_path: str, mods_dir: str) -> str:
    """Move a superseded zip into '<mods>/.replaced/' (kept, hidden from
    scans). Returns the new location."""
    dest_dir = Path(mods_dir) / ".replaced"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(zip_path).name
    if dest.exists():
        dest = dest_dir / (
            Path(zip_path).stem + time.strftime(" %Y-%m-%d %H%M%S") + ".zip"
        )
    shutil.move(zip_path, dest)
    return str(dest)


def _mod_entries(root: Path, kind: str) -> list[str]:
    """All file entries of a mod, '/'-separated, relative to its root."""
    if kind == "zip":
        with ZipFile(root) as z:
            return [n for n in z.namelist() if not n.endswith("/")]
    return [
        str(f.relative_to(root)).replace("\\", "/")
        for f in sorted(root.rglob("*"))
        if f.is_file()
    ]


def scan_mods_worker(
    log, mods_dir: str, index: dict, keep_both: list | str | None = None
) -> dict:
    """Resolve every mod (zip or loose folder) in the mods folder.

    ``keep_both`` (raw QSettings value) lists zip-filename pairs the user
    chose to keep as separate mods instead of replacing the older archive."""
    if isinstance(keep_both, str):
        keep_both = json.loads(keep_both) if keep_both else []
    hashmap = index_paks.build_hashmap(index)
    sources = _mod_sources(Path(mods_dir), keep_both)
    if not sources:
        log(f"No mods (zips or folders) found in {mods_dir}")
        return {"mods": [], "collisions": {}}

    mods: list[ModScan] = []
    owners: dict[str, list[str]] = {}  # stored pak path -> [mod names]
    for root, mod_name, kind, superseded in sources:
        scan = ModScan(
            name=mod_name, path=str(root), kind=kind,
            superseded_zips=superseded,
        )
        for entry in _mod_entries(root, kind):
            base = entry.rsplit("/", 1)[-1]
            if base.lower() == "mod_info.json":
                continue
            # Zip entries sit under a wrapper folder named after the mod;
            # loose-folder entries are already relative to the mod root.
            rel = locate_mod.game_rel_path(entry) if kind == "zip" else entry
            if rel.lower().endswith(tuple(sorted(locate_mod.SKIP_EXT))):
                scan.hits.append(FileHit(entry=entry, rel=rel, skipped=True))
                continue
            cand, hit = locate_mod.resolve(rel, hashmap)
            if hit:
                pak, stored = hit
                scan.hits.append(
                    FileHit(
                        entry=entry,
                        rel=rel,
                        candidate=cand,
                        pak=pak,
                        stored=stored,
                        match=locate_mod.describe_match(rel, cand),
                    )
                )
                owners.setdefault(stored, []).append(mod_name)
            else:
                scan.hits.append(FileHit(entry=entry, rel=rel))
        mods.append(scan)

    collisions = {p: names for p, names in owners.items() if len(names) > 1}
    total_files = sum(len(m.game_files) for m in mods)
    log(f"Resolved {len(mods)} mod(s), {total_files} game file(s), "
        f"{len(collisions)} colliding pak path(s)")
    return {"mods": mods, "collisions": collisions}


def verify_worker(log, scan: dict, index: dict, only_mod: str | None = None) -> dict:
    """Verify every resolved mod file against the vanilla pak data.

    With ``only_mod`` set, verify just that mod (targeted post-save refresh)."""
    return verify_mods.verify_scan(scan, index, log, only_mod=only_mod)


def compiler_status_worker(log, force: bool = False) -> dict:
    """Game version, newest local compiler, and the release the update policy
    would select (newest MBINCompiler release <= game version).

    Network/PowerShell failures are reported in the result, not raised, so the
    GUI always gets version info to display. ``force`` bypasses the release
    cache (the GUI's manual re-check)."""
    game = mbincompiler.game_version()
    local = mbincompiler.local_compilers()
    result: dict = {
        "game": game,
        "local": local[0] if local else None,
        "locals": [c["tag"] for c in local],
    }
    log("Checking for compiler updates…" if force else "Checking for compiler updates (cached)…")
    try:
        best = mbincompiler.pick_release(
            mbincompiler.list_remote_releases(force=force), game
        )
        result["remote"] = (
            {"tag": best["tag"], "version": best["version"], "asset_url": best["asset_url"]}
            if best
            else None
        )
        result["cached_check"] = not force
    except Exception as exc:  # noqa: BLE001 - offline / rate limited
        result["remote_error"] = friendly_error(exc)
    return result


def compiler_update_worker(log, release: dict) -> dict:
    """Download one MBINCompiler release into the managed folder."""
    log(f"Downloading MBINCompiler {release['tag']}…")
    entry = mbincompiler.download(release)
    log(f"Downloaded MBINCompiler {entry['tag']}")
    return entry


def _find_mod(scan: dict, mod_name: str):
    mod = next((m for m in scan["mods"] if m.name == mod_name), None)
    if mod is None:
        raise RuntimeError(f"mod {mod_name!r} not found in the current scan")
    return mod


def inject_mod_worker(log, mod_name: str, scan: dict, index: dict, pcbanks: str) -> dict:
    """Bake one mod's files into the game paks (background)."""
    mod = _find_mod(scan, mod_name)
    log(f"Injecting {mod_name} ({len(mod.game_files)} game file(s))…")
    return inject_mods.inject_mod(mod, index, pcbanks, log)


def revert_mod_worker(log, mod_name: str, scan: dict, index: dict, pcbanks: str) -> dict:
    """Restore one mod's pre-injection bytes (background)."""
    log(f"Reverting {mod_name}…")
    return inject_mods.revert_mod(mod_name, scan, index, pcbanks, log)


def install_mod_worker(log, mod_name: str, scan: dict, pcbanks: str) -> dict:
    """Copy one mod's files into the game's loose-mod folder (background)."""
    mod = _find_mod(scan, mod_name)
    log(f"Installing {mod_name} to GAMEDATA\\MODS…")
    return install_mods.install_mod(mod, pcbanks, log)


def uninstall_mod_worker(log, mod_name: str, scan: dict, pcbanks: str) -> dict:
    """Remove one mod's folder from the loose-mod loader (background)."""
    return install_mods.uninstall_mod(mod_name, pcbanks, log)


def revert_all_worker(log, scan: dict, index: dict, pcbanks: str) -> dict:
    """Revert every injected mod back to vanilla (background)."""
    return inject_mods.revert_all(scan, index, pcbanks, log)


def merge_preview_worker(log, mod_names: list, scan: dict, index: dict,
                         pcbanks: str) -> dict:
    """Compute what a group injection would do, including conflicts."""
    log(f"Computing merge for {len(mod_names)} mod(s)…")
    return inject_mods.merge_preview(mod_names, scan, index, pcbanks, log)


def inject_group_worker(log, mod_names: list, scan: dict, index: dict,
                        pcbanks: str, resolutions: dict | None) -> dict:
    """Inject several mods with their shared files merged (background)."""
    log(f"Injecting {len(mod_names)} mod(s) as a merged group…")
    return inject_mods.inject_group(mod_names, scan, index, pcbanks, resolutions, log)


def inject_all_worker(log, names: list, scan: dict, index: dict, pcbanks: str,
                      resolutions: dict | None) -> dict:
    """Inject every eligible mod, merging colliding groups (background)."""
    # Connected components over shared pak paths.
    group_of = {n: n for n in names}

    def find(n):
        while group_of[n] != n:
            group_of[n] = group_of[group_of[n]]
            n = group_of[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            group_of[rb] = ra

    for _stored, owners in scan["collisions"].items():
        members = [n for n in owners if n in group_of]
        for other in members[1:]:
            union(members[0], other)
    components: dict[str, list] = {}
    for n in names:  # preserve the GUI's eligibility order
        components.setdefault(find(n), []).append(n)

    totals = {"injected": 0, "skipped": [], "groups": 0}
    for members in components.values():
        if len(members) == 1:
            mod = next(m for m in scan["mods"] if m.name == members[0])
            log(f"Injecting {members[0]}…")
            result = inject_mods.inject_mod(mod, index, pcbanks, log)
            totals["injected"] += result["injected"]
            totals["skipped"].extend(result["skipped"])
        else:
            log(f"Injecting merged group: {', '.join(members)}…")
            result = inject_mods.inject_group(
                members, scan, index, pcbanks, resolutions, log
            )
            totals["injected"] += result["injected"]
            totals["skipped"].extend(result["skipped"])
            totals["groups"] += 1
    log(
        f"Inject all complete: {totals['injected']} file(s) baked, "
        f"{totals['groups']} merged group(s), {len(totals['skipped'])} skipped"
    )
    return totals

