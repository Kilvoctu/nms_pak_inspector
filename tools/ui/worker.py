"""Background task workers: TaskWorker runs fn(log, *args) on a QThread, result via done(object)."""

import json
import os
import shutil
import time
import xml.etree.ElementTree as ET
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
    nexus,
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
    note: str | None = None  # why this hit is skipped (e.g. "user-excluded")


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
    """Run ``fn(log, *args)`` in the background; signals deliver to the GUI thread."""

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
    """The single top-level folder inside a zip (Nexus-style wrapper = better display name).

    None at zip root (except loose .lua/.txt), multiple folders, or game-content dirs."""
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
    """Enumerate mods (zips + folders); returns (root, display_name, kind, superseded_zips).

    Zips are named by their inner folder; same-inner zips are versions of one mod (newest wins, keep_both exempt)."""
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

    # Group zips by inner wrapper name (Nexus-style version updates), then
    # merge groups sharing a Nexus mod id: wrapper names may embed the
    # version (or get renamed between updates) - the mod id is stable.
    by_inner: dict[str, list[Path]] = {}
    stem_only: list[Path] = []
    for zpath, inner in zip_wrappers.items():
        if inner and inner not in used:
            by_inner.setdefault(inner, []).append(zpath)
        else:
            stem_only.append(zpath)
    parent: dict[str, str] = {}

    def _find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_id: dict[int, list[str]] = {}
    for inner, zips in by_inner.items():
        for z in zips:
            parsed = nexus.parse_nexus_filename(z)
            if parsed:
                by_id.setdefault(parsed["mod_id"], []).append(inner)
    for inners in by_id.values():
        for inner in inners[1:]:
            root_a, root_b = _find(inners[0]), _find(inner)
            if root_a != root_b:
                parent[root_b] = root_a
    merged: dict[str, list[Path]] = {}
    for inner, zips in by_inner.items():
        merged.setdefault(_find(inner), []).extend(zips)

    def _older_display(p: Path) -> str:
        """Older siblings show their inner wrapper name (falling back to stem)."""
        d = _zip_wrapper_name(p) or p.stem
        return p.stem if d in used else d

    for inner, zips in sorted(merged.items()):
        ordered = sorted(zips, key=lambda p: (os.path.getmtime(p), p.name))
        newest = ordered[-1]
        older = ordered[:-1]
        # The newest zip's wrapper is the group identity: display (and loose
        # manifest migration) follow the update, not the union-find root.
        display = _zip_wrapper_name(newest) or inner
        if len(zips) == 1:
            stash(zips[0], display, [])
            used.add(display)
            continue
        pair = sorted([p.name for p in zips])
        if any(pair == sorted(k) for k in (keep_both or [])):
            # 'Keep both': the newest keeps the group identity, older
            # archives stay as separate mods under their own names.
            stash(newest, display, [])
            used.add(display)
            for p in older:
                d = _older_display(p)
                stash(p, d, [])
                used.add(d)
            continue
        older_names = sorted(str(p) for p in older)
        stash(newest, display, older_names)
        used.add(display)
        for p in older:
            d = _older_display(p)
            stash(p, d, [])
            used.add(d)
    for zpath in stem_only:
        display = zpath.stem
        if display in used:
            display = f"{display} ({zpath.stat().st_size})"
        stash(zpath, display, [])
        used.add(display)
    return sources


def stash_replaced_zip(zip_path: str, mods_dir: str) -> str:
    """Move a superseded zip into '<mods>/.replaced/' (kept, hidden from scans). Returns the new location."""
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
    """Resolve every mod (zip or loose folder); ``keep_both`` = pairs kept as separate mods."""
    if isinstance(keep_both, str):
        keep_both = json.loads(keep_both) if keep_both else []
    hashmap = index_paks.build_hashmap(index)
    sources = _mod_sources(Path(mods_dir), keep_both)
    if not sources:
        log(f"No mods (zips or folders) found in {mods_dir}")
        return {"mods": [], "collisions": {}}

    mods: list[ModScan] = []
    owners: dict[str, list[str]] = {}  # stored pak path -> [mod names]
    root_dirs = {
        p.split("/", 1)[0].lower()
        for info in index.get("paks", {}).values()
        for p in info.get("files", [])
        if "/" in p
    }
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

        unresolved = [h for h in scan.hits if h.pak is None and not h.skipped]
        sib_dirs: dict[str, str] = {}
        for h in scan.hits:
            if h.stored and "/" in h.stored:
                sib_dirs[h.stored.rsplit("/", 1)[0]] = h.pak
        if unresolved and len(sib_dirs) == 1:
            base, pak = next(iter(sib_dirs.items()))
            for h in unresolved:
                first = h.rel.replace("\\", "/").split("/", 1)[0].lower()
                if first in root_dirs:
                    continue  # pak-root anchored: a plain new file
                h.pak = pak
                h.stored = f"{base}/{h.rel}".replace("\\", "/").lower()
                h.match = f"scene-relative to {base}/"
        # User exclusions: skipped files drop out of verification and injection
        # (the mod archive itself is never modified).
        _ignored, excluded = verify_mods.load_overrides()
        if excluded:
            for h in scan.hits:
                if f"{mod_name}|{h.entry}" in excluded:
                    h.skipped = True
                    h.note = "user-excluded"
        mods.append(scan)
    collisions = {p: names for p, names in owners.items() if len(names) > 1}
    total_files = sum(len(m.game_files) for m in mods)
    log(f"Resolved {len(mods)} mod(s), {total_files} game file(s), "
        f"{len(collisions)} colliding pak path(s)")
    return {"mods": mods, "collisions": collisions}


def verify_worker(log, scan: dict, index: dict, only_mod: str | None = None) -> dict:
    """Verify every resolved mod file (``only_mod`` = targeted post-save refresh)."""
    return verify_mods.verify_scan(scan, index, log, only_mod=only_mod)


def compiler_status_worker(log, force: bool = False) -> dict:
    """Game version + newest local/remote compiler (failures reported, not raised)."""
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


def audit_worker(log, pcbanks: str) -> dict:
    """Static crash-risk audit of every injected file (background)."""
    log("Auditing injected files (decompile round-trips, DDS headers, pak integrity)…")
    from .. import audit_injected

    return audit_injected.audit_injections(pcbanks, log)


def install_mod_worker(log, mod_name: str, scan: dict, index: dict, pcbanks: str) -> dict:
    """Add one mod to the managed loose install and rebuild the folder (background)."""
    mod = _find_mod(scan, mod_name)
    log(f"Installing {mod_name} to GAMEDATA\\MODS\\{install_mods.MANAGED_FOLDER}…")
    return install_mods.install_mod(mod, scan, index, pcbanks, log)


def uninstall_mod_worker(log, mod_name: str, scan: dict, index: dict, pcbanks: str) -> dict:
    """Remove one mod from the managed loose install and rebuild (background)."""
    return install_mods.uninstall_mod(mod_name, scan, index, pcbanks, log)


def remove_loose_worker(log, scan: dict, index: dict, pcbanks: str) -> dict:
    """Wipe the managed loose-install folder and manifest (background)."""
    return install_mods.remove_all(pcbanks, log)


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


def nexus_info_worker(log, api_key: str, source_path: str, display_name: str) -> dict:
    """Fetch Nexus metadata for one mod archive; errors reported in the result."""
    parsed = nexus.parse_nexus_filename(source_path)
    if parsed is None:
        return {"parsed": None}
    mod_id = parsed["mod_id"]
    log(f"Fetching Nexus info for mod {mod_id}…")
    result: dict = {"parsed": parsed, "mod": None, "latest": None}
    try:
        result["mod"] = nexus.fetch_mod(mod_id, api_key)
        files = nexus.fetch_files(mod_id, api_key)
        base_names = {
            nexus.get_base_name(parsed["name"]),
            nexus.get_base_name(display_name),
        } - {""}
        latest, matched = nexus.latest_file_for(files, base_names)
        result["latest"] = latest
        result["family_match"] = matched
    except Exception as exc:  # noqa: BLE001 - shown in the dialog
        result["error"] = friendly_error(exc)
    return result


def nexus_updates_worker(log, api_key: str, scan: dict) -> dict:
    """Check every Nexus-sourced mod for updates (background, cached requests)."""
    updates, skipped, errors = [], [], []
    checked = 0
    mods = scan["mods"] if scan else []
    for i, mod in enumerate(mods, 1):
        parsed = nexus.parse_nexus_filename(mod.path)
        if parsed is None:
            skipped.append(mod.name)
            continue
        try:
            result = nexus.check_update(parsed, mod.name, api_key)
        except Exception as exc:  # noqa: BLE001 - collected per mod
            errors.append((mod.name, friendly_error(exc)))
            continue
        checked += 1
        if result["newer"]:
            latest = result["latest"]
            updates.append({
                "name": mod.name,
                "mod_id": parsed["mod_id"],
                "version": parsed["version"],
                "latest_version": latest.get("version") or "?",
                "uploaded": latest.get("uploaded_timestamp") or 0,
            })
            log(f"update available: {mod.name} {parsed['version']} -> {updates[-1]['latest_version']}")
        if i % 10 == 0:
            log(f"checked {i}/{len(mods)} mods…")
    return {"updates": updates, "checked": checked, "skipped": skipped, "errors": errors}


def fields_prep_worker(log, pcbanks: str, pak: str, stored: str, index: dict):
    """Extract + decompile the vanilla MBIN for the editor (off the GUI thread).

    Returns the parsed vanilla root element."""
    log(f"Preparing editor for {stored}…")
    van_mxml = verify_mods._extract_and_decompile(
        pcbanks, pak, stored, index, log, mbincompiler.ensure_compiler()
    )
    return ET.parse(van_mxml).getroot()

