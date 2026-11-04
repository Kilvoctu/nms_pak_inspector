"""Bake mods into the game's paks with pre-image backups and byte-exact revert (see inject)."""

import hashlib
import json
import os
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import mbincompiler, merge_mods, paklib, repo
from .verify_mods import (
    _extract_and_decompile,
    _parse_mod_tree,
    _read_mod_data,
    is_raw_asset,
)

STATE_FILE = repo.inject_state_file()
BACKUP_DIR = repo.inject_backup_dir()
WORK_ROOT = repo.cache_dir() / "_injectwork"


# --- state ---------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    if state:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        STATE_FILE.unlink(missing_ok=True)  # keep the project root clean


def clear_state() -> None:
    """Forget all injections and delete pre-image backups (game updated = vanilla again)."""
    STATE_FILE.unlink(missing_ok=True)
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)


def _backup_path(pak: str, stored: str, event: str = "") -> Path:
    key = hashlib.md5(f"{pak}|{stored}".encode()).hexdigest()[:16]
    return BACKUP_DIR / f"{key}{event}.bin"


def _take_backup_path(pak: str, stored: str) -> Path:
    """First free backup name for a path (event-unique, never overwrites a
    backup an existing state entry still references)."""
    candidate = _backup_path(pak, stored)
    n = 2
    while candidate.exists():
        candidate = _backup_path(pak, stored, f".{n}")
        n += 1
    return candidate


def injected_paths() -> set[tuple[str, str]]:
    """All (pak, stored) paths currently baked into the paks."""
    out: set[tuple[str, str]] = set()
    for entry in load_state().values():
        for f in entry.get("files", []):
            out.add((f["pak"], f["stored"]))
    return out


# --- injection -----------------------------------------------------------------


def _content_for(mod, hit, index: dict, pcbanks: str, work: Path, log, compiler) -> bytes:
    """Final pak content for one mod file: merged MBIN for EXML mods, raw
    bytes for everything else (shipped MBINs, textures, ...)."""
    data = _read_mod_data(mod, hit.entry)
    if hit.entry.lower().endswith((".exml", ".mxml")):
        van_mxml = _extract_and_decompile(pcbanks, hit.pak, hit.stored, index, log, compiler)
        van_root = _vanilla_root(van_mxml)
        mod_root = _parse_mod_tree(hit.entry, data, work, log)
        stem = hashlib.md5(hit.entry.encode()).hexdigest()[:12]
        return merge_mods.build_mbin(mod_root, van_root, work, stem, log)
    return data


def _vanilla_root(van_mxml: Path):
    return ET.parse(van_mxml).getroot()


def _swap_pak(pak_path: Path, changes: dict[str, bytes], log) -> dict:
    """Rewrite one pak with ``changes`` and atomically swap it in."""
    tmp = pak_path.with_suffix(".pak.injecting")
    name = pak_path.name
    def report(done: int, total: int) -> None:
        # 10% steps, plus a 20s heartbeat for slow stretches (huge paks crawl).
        step = done * 100 // total
        now = time.monotonic()
        if step > _state[0] or now - _state[1] > 20:
            _state[0], _state[1] = step, now
            log(f"re-chunking {name}: {step}% ({done:,}/{total:,} windows)")

    _state = [-1, 0.0]
    stats = paklib.inject(pak_path, changes, tmp, progress=report)
    os.replace(tmp, pak_path)
    # Keep drift baselines in sync with intentional content so future audits
    # only flag unexplained changes.
    from . import audit_injected

    audit_injected.update_baseline(pak_path.name, changes)
    log(
        f"wrote {pak_path.name}: {stats['files']} file(s), "
        f"{stats['chunks_copied']} chunk(s) copied verbatim, "
        f"{stats['chunks_recompressed']} recompressed, "
        f"{stats['size_before'] / 1e6:.1f} -> {stats['size_after'] / 1e6:.1f} MB"
    )
    return stats


def _refresh_index(index: dict, pak_name: str, pak_path: Path) -> None:
    """Track our own injection in the index (``generated`` cache key must NOT change)."""
    info = index.get("paks", {}).get(pak_name)
    if info is not None:
        st = os.stat(pak_path)
        info["size"] = st.st_size
        info["mtime"] = int(st.st_mtime)
        with open(repo.index_file(), "w", encoding="utf-8") as f:
            json.dump(index, f, separators=(",", ":"))


def inject_mod(mod, index: dict, pcbanks: str, log) -> dict:
    """Bake one mod's resolved files into the paks. Returns counts."""
    state = load_state()
    # Guard: paths already injected by other mods need the merge flow.
    taken = injected_paths()
    mine = {(h.pak, h.stored) for h in mod.game_files if h.pak and h.stored}
    clash = mine & taken
    if clash:
        owners = sorted({
            name for name, entry in state.items()
            if any((f["pak"], f["stored"]) in clash for f in entry.get("files", []))
        })
        raise RuntimeError(
            f"{len(clash)} file(s) already injected by {', '.join(owners)} - "
            "revert those first or use the merge injection"
        )
    compiler = mbincompiler.ensure_compiler()
    work = WORK_ROOT / hashlib.md5(mod.name.encode()).hexdigest()[:12]
    work.mkdir(parents=True, exist_ok=True)

    total = len(mod.game_files)
    prepared: dict[str, dict[str, tuple[bytes, str]]] = {}  # pak -> stored -> (bytes, rel)
    skipped: list[tuple[str, str]] = []
    done = 0
    for hit in mod.game_files:
        done += 1
        if not (hit.pak and hit.stored):
            skipped.append((hit.rel, "no matching pak entry"))
            log(f"[{done}/{total}] {hit.rel}: skipped - no matching pak entry")
            continue
        try:
            data = _content_for(mod, hit, index, pcbanks, work, log, compiler)
        except merge_mods.MergeError as exc:
            skipped.append((hit.rel, str(exc)))
            log(f"[{done}/{total}] {hit.rel}: skipped - {exc}")
            continue
        if hit.stored in prepared.get(hit.pak, {}):
            skipped.append((hit.rel, "duplicate pak path in this mod"))
            continue
        prepared.setdefault(hit.pak, {})[hit.stored] = (data, hit.rel)

    # State entry grows per pak as each swap completes, so a failure partway
    # through still leaves an accurate record (and revertable backups).
    entry = {
        "order": max((s.get("order", 0) for s in state.values()), default=0) + 1,
        "injected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [f for f in state.get(mod.name, {}).get("files", [])],
    }
    state[mod.name] = entry

    injected: list[dict] = []
    for pak_name, entries in sorted(prepared.items()):
        pak_path = Path(pcbanks) / pak_name
        backups: dict[str, str] = {}
        changes: dict[str, bytes] = {}
        # Snapshot phase: read pre-images, then close the pak before the
        # swap (Windows refuses os.replace while a handle is open).
        src = paklib.Pak(pak_path)
        try:
            for stored, (data, _rel) in entries.items():
                if stored not in src.file_map:
                    skipped.append((_rel, "new file - pak injection cannot add files"))
                    log(
                        f"{_rel}: skipped - new file (pak injection adds no files; "
                        "loose-install this mod instead)"
                    )
                    continue
                backup = _take_backup_path(pak_name, stored)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(src.read_file(stored))
                backups[stored] = backup.name
                changes[stored] = data
        finally:
            src.close()
        if changes:
            _swap_pak(pak_path, changes, log)
        for stored, (data, rel) in entries.items():
            if stored in changes:
                injected.append(
                    {"pak": pak_name, "stored": stored, "rel": rel,
                     "backup": backups[stored]}
                )
                log(f"baked {rel} into {pak_name}")
        entry["files"] = injected
        save_state(state)
        _refresh_index(index, pak_name, pak_path)

    shutil.rmtree(work, ignore_errors=True)

    if not injected:
        del state[mod.name]
        save_state(state)
    log(
        f"Injected {mod.name}: {len(injected)} file(s) baked, "
        f"{len(skipped)} skipped, {len(prepared)} pak(s) touched"
    )
    return {"injected": len(injected), "skipped": skipped, "paks": sorted(prepared)}


def revert_mod(mod_name: str, scan: dict, index: dict, pcbanks: str, log,
               reapply: bool = True) -> dict:
    """Restore pre-injection bytes; ``reapply`` re-injects other mods sharing those paths."""
    state = load_state()
    entry = state.pop(mod_name, None)
    if entry is None:
        raise RuntimeError(f"{mod_name} is not injected")
    files = entry["files"]

    restores: dict[str, dict[str, bytes]] = {}
    for f in files:
        backup = BACKUP_DIR / f["backup"]
        if not backup.exists():
            raise RuntimeError(f"pre-injection backup missing for {f['rel']}")
        restores.setdefault(f["pak"], {})[f["stored"]] = backup.read_bytes()

    for pak_name, changes in sorted(restores.items()):
        pak_path = Path(pcbanks) / pak_name
        _swap_pak(pak_path, changes, log)
        for stored in changes:
            log(f"restored {stored} in {pak_name}")
        _refresh_index(index, pak_name, pak_path)

    reverted = {(f["pak"], f["stored"]) for f in files}
    for f in files:
        (BACKUP_DIR / f["backup"]).unlink(missing_ok=True)
    save_state(state)
    log(f"Reverted {mod_name}: {len(files)} file(s) restored to their pre-injection bytes")

    # Re-apply the merged content of the still-injected mods over the
    # reverted paths so surviving edits are not clobbered.
    if not reapply:
        return {"restored": len(files), "reapplied": []}
    reapplied = reapply_merged(reverted, scan, index, pcbanks, log)
    return {"restored": len(files), "reapplied": reapplied}


def revert_all(scan: dict, index: dict, pcbanks: str, log) -> dict:
    """Revert every injected mod newest-first (reverse order unwinds the chain to exact vanilla)."""
    state = load_state()
    names = sorted(state, key=lambda n: state[n].get("order", 0), reverse=True)
    for name in names:
        revert_mod(name, scan, index, pcbanks, log, reapply=False)
    log(f"Restore vanilla complete: {len(names)} injected mod(s) reverted")
    return {"reverted": len(names)}


# --- group (merged) injection ----------------------------------------------------


def _merged_content(participants: list, van_root, work: Path, resolutions,
                    log) -> bytes:
    """Merged MBIN bytes for one pak path; ``participants`` is [(mod, hit)]."""
    mod_roots = [
        _parse_mod_tree(
            hit.entry, _read_mod_data(mod, hit.entry), work, log
        )
        for mod, hit in participants
    ]
    merged, notes, _conflicts = merge_mods.merge_many(
        mod_roots, van_root, resolutions
    )
    for note in notes:
        log(f"merge: {note}")
    stem = hashlib.md5(
        "|".join(hit.entry for _mod, hit in participants).encode()
    ).hexdigest()[:12]
    return merge_mods.merged_to_mbin(merged, work, stem)


def _raw_bytes_for(parts: list, resolutions: dict | None, stored: str, log) -> tuple[bytes | None, str]:
    """Content for a shared raw (non-MBIN) pak path: identical bytes pass through.

    Differing bytes need a dialog winner (``resolutions`` keyed by stored path); else (None, reason)."""
    datas = [_read_mod_data(mod, hit.entry) for mod, hit in parts]
    if len(set(datas)) == 1:
        return datas[0], f"raw file, identical bytes from {len(parts)} mod(s)"
    winner = (resolutions or {}).get(stored)
    if winner is None or not 0 <= winner < len(datas):
        names = ", ".join(m.name for m, _h in parts)
        return None, f"raw bytes differ between {names} - needs a merge-dialog winner"
    return datas[winner], f"raw bytes, winner: {parts[winner][0].name}"


def _raw_preview_entry(parts: list) -> dict:
    """Preview entry for a raw (non-MBIN) path; differing bytes become a winner choice."""
    datas = [_read_mod_data(mod, hit.entry) for mod, hit in parts]
    conflicts = []
    if len(set(datas)) > 1:
        conflicts = [
            merge_mods.MergeConflict(
                path=parts[0][1].stored,
                choices=[
                    (i, f"{m.name}: {len(d):,} bytes")
                    for i, (d, (m, _h)) in enumerate(zip(datas, parts))
                ],
            )
        ]
    hit0 = parts[0][1]
    return {
        "pak": hit0.pak,
        "stored": hit0.stored,
        "participants": [m.name for m, _h in parts],
        "conflicts": conflicts,
        "raw": True,
    }


def merge_preview(mod_names: list[str], scan: dict, index: dict, pcbanks: str,
                  log) -> dict:
    """Compute what a group injection WOULD do (no injection, no state)."""
    mods = []
    for name in mod_names:
        mod = next((m for m in scan["mods"] if m.name == name), None)
        if mod is None:
            raise RuntimeError(f"mod {name!r} not found in the current scan")
        mods.append(mod)
    compiler = mbincompiler.ensure_compiler()
    work = WORK_ROOT / "preview"
    work.mkdir(parents=True, exist_ok=True)

    participants: dict[tuple[str, str], list] = {}
    for mod in mods:
        for hit in mod.game_files:
            if hit.pak and hit.stored:
                participants.setdefault((hit.pak, hit.stored), []).append((mod, hit))

    files: dict[str, dict] = {}
    for (pak_name, stored), parts in sorted(participants.items()):
        if is_raw_asset(parts[0][1].entry):
            files[f"{pak_name}|{stored}"] = _raw_preview_entry(parts)
            continue
        van_mxml = _extract_and_decompile(pcbanks, pak_name, stored, index, log, compiler)
        van_root = _vanilla_root(van_mxml)
        mod_roots = [
            _parse_mod_tree(hit.entry, _read_mod_data(mod, hit.entry), work, log)
            for mod, hit in parts
        ]
        merged, notes, conflicts = merge_mods.merge_many(mod_roots, van_root, None)
        files[f"{pak_name}|{stored}"] = {
            "pak": pak_name,
            "stored": stored,
            "participants": [m.name for m, _h in parts],
            "conflicts": conflicts,
            "mod_roots": mod_roots,
            "van_root": van_root,
            "merged_root": merged,
            "notes": notes,
        }
    shutil.rmtree(work, ignore_errors=True)
    return {"mod_names": list(mod_names), "files": files}


def inject_group(mod_names: list[str], scan: dict, index: dict, pcbanks: str,
                 resolutions: dict[str, int] | None, log) -> dict:
    """Inject several mods at once, merging their edits per shared pak path."""
    state = load_state()
    mods = []
    for name in mod_names:
        mod = next((m for m in scan["mods"] if m.name == name), None)
        if mod is None:
            raise RuntimeError(f"mod {name!r} not found in the current scan")
        mods.append(mod)
    compiler = mbincompiler.ensure_compiler()
    event = hashlib.md5(
        ("|".join(sorted(mod_names)) + datetime.now(timezone.utc).isoformat())
        .encode()
    ).hexdigest()[:8]
    work = WORK_ROOT / f"group-{event}"
    work.mkdir(parents=True, exist_ok=True)

    # (pak, stored) -> [(mod, hit)] participants, in the given mod order.
    participants: dict[tuple[str, str], list] = {}
    for mod in mods:
        for hit in mod.game_files:
            if hit.pak and hit.stored:
                participants.setdefault((hit.pak, hit.stored), []).append((mod, hit))

    total = len(participants)
    done = 0
    prepared: dict[str, dict[str, bytes]] = {}
    skipped: list[tuple[str, str]] = []
    for (pak_name, stored), parts in sorted(participants.items()):
        done += 1
        rel0 = parts[0][1].rel
        if is_raw_asset(rel0):
            data, note = _raw_bytes_for(parts, resolutions, stored, log)
            if data is None:
                skipped.append((rel0, note))
                log(f"[{done}/{total}] {rel0}: skipped - {note}")
                continue
            prepared.setdefault(pak_name, {})[stored] = data
            log(f"[{done}/{total}] {rel0}: {note}")
            continue
        try:
            van_mxml = _extract_and_decompile(
                pcbanks, pak_name, stored, index, log, compiler
            )
            data = _merged_content(parts, _vanilla_root(van_mxml), work,
                                   resolutions, log)
        except merge_mods.MergeError as exc:
            skipped.append((rel0, str(exc)))
            log(f"[{done}/{total}] {rel0}: skipped - {exc}")
            continue
        prepared.setdefault(pak_name, {})[stored] = data
        note = f"merged {len(parts)} mod edit(s)" if len(parts) > 1 else "merged"
        log(f"[{done}/{total}] {rel0}: {note}")

    # Snapshot + swap per pak, recording state after each pak so a failure
    # partway leaves accurate records (same pattern as inject_mod).
    injected_paths_done: set[tuple[str, str]] = set()
    all_backups: dict[tuple[str, str], str] = {}
    for pak_name, entries in sorted(prepared.items()):
        pak_path = Path(pcbanks) / pak_name
        changes: dict[str, bytes] = {}
        src = paklib.Pak(pak_path)
        try:
            for stored, data in entries.items():
                if stored not in src.file_map:
                    continue
                backup = _take_backup_path(pak_name, stored)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(src.read_file(stored))
                all_backups[(pak_name, stored)] = backup.name
                changes[stored] = data
        finally:
            src.close()
        if not changes:
            continue
        _swap_pak(pak_path, changes, log)
        _refresh_index(index, pak_name, pak_path)
        injected_paths_done.update((pak_name, stored) for stored in changes)
        # State: new members get entries referencing this event's backups;
        # already-injected members keep theirs (their own backups stay valid).
        baked = {(pak_name, stored) for stored in changes}
        next_order = max((s.get("order", 0) for s in state.values()), default=0)
        for mod in mods:
            files = state.get(mod.name, {}).get("files", [])
            have = {(f["pak"], f["stored"]) for f in files}
            added = [
                {"pak": hit.pak, "stored": hit.stored, "rel": hit.rel,
                 "backup": all_backups[(hit.pak, hit.stored)]}
                for hit in mod.game_files
                if (hit.pak, hit.stored) in baked and (hit.pak, hit.stored) not in have
            ]
            if not added:
                continue
            if mod.name not in state:
                next_order += 1
                state[mod.name] = {
                    "order": next_order,
                    "injected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": files + added,
                }
            else:
                state[mod.name]["files"] = files + added
        save_state(state)

    baked_counts = {
        mod.name: len(state.get(mod.name, {}).get("files", [])) for mod in mods
    }
    shutil.rmtree(work, ignore_errors=True)

    log(
        f"Group injection complete: {len(injected_paths_done)} file(s) baked "
        f"across {len(mods)} mod(s), {len(skipped)} skipped"
    )
    return {
        "injected": len(injected_paths_done),
        "skipped": skipped,
        "baked_counts": baked_counts,
    }


def reapply_merged(reverted_paths: set[tuple[str, str]], scan: dict, index: dict,
                   pcbanks: str, log) -> list[str]:
    """Re-inject the merged content of the still-injected mods over paths
    that were just reverted, so surviving edits are not clobbered."""
    state = load_state()
    if not state or not reverted_paths:
        return []
    # Still-injected mods touching the reverted paths, in injection order.
    participants: dict[tuple[str, str], list[str]] = {}
    for name in sorted(state, key=lambda n: state[n].get("order", 0)):
        for f in state[name].get("files", []):
            key = (f["pak"], f["stored"])
            if key in reverted_paths:
                participants.setdefault(key, []).append(name)
    if not participants:
        return []
    compiler = mbincompiler.ensure_compiler()
    work = WORK_ROOT / "reapply"
    work.mkdir(parents=True, exist_ok=True)

    # Resolve each participant mod once; skip mods whose source is gone.
    mod_objs: dict[str, object] = {}
    for name in sorted({n for names in participants.values() for n in names}):
        mod = next((m for m in scan["mods"] if m.name == name), None)
        if mod is None:
            log(f"cannot re-apply {name}: mod folder/zip is gone")
        else:
            mod_objs[name] = mod

    changes_by_pak: dict[str, dict[str, bytes]] = {}
    for (pak_name, stored), names in sorted(participants.items()):
        parts = []
        for n in names:
            mod = mod_objs.get(n)
            hit = _hit_for(mod, pak_name, stored) if mod is not None else None
            if hit is not None:
                parts.append((mod, hit))
        if not parts:
            continue
        if is_raw_asset(parts[0][1].entry):
            data, note = _raw_bytes_for(parts, None, stored, log)
            if data is None:
                log(f"re-apply of {stored} skipped - {note}")
                continue
            changes_by_pak.setdefault(pak_name, {})[stored] = data
            continue
        try:
            van_mxml = _extract_and_decompile(
                pcbanks, pak_name, stored, index, log, compiler
            )
            data = _merged_content(parts, _vanilla_root(van_mxml), work, None, log)
        except merge_mods.MergeError as exc:
            log(f"re-apply of {stored} skipped - {exc}")
            continue
        changes_by_pak.setdefault(pak_name, {})[stored] = data
        log(f"re-applying {len(parts)} injected mod(s) over {stored}")

    for pak_name, changes in sorted(changes_by_pak.items()):
        pak_path = Path(pcbanks) / pak_name
        _swap_pak(pak_path, changes, log)
        _refresh_index(index, pak_name, pak_path)
    shutil.rmtree(work, ignore_errors=True)
    return sorted({n for names in participants.values() for n in names})


def _hit_for(mod, pak_name: str, stored: str):
    """A mod's hit for one stored pak path (None when it doesn't have one)."""
    return next(
        (h for h in mod.game_files if h.pak == pak_name and h.stored == stored),
        None,
    )
