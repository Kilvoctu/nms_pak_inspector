"""Verify loose mods against the actual vanilla data (a dry-run merge per file; see verdicts below)."""

import hashlib
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from . import mbincompiler, paklib, repo

CACHE_DIR = repo.vanilla_cache_dir()

# Verdicts
V_OK = "OK"
V_FORMAT = "FORMAT"
V_MISSING = "MISSING"
V_TEMPLATE = "TEMPLATE"
V_NOOP = "NO-OP"
# Editor-only verdicts (iter_field_rows): verify's diff never emits these -
# it skips unchanged fields and reports directives as OK.
V_SAME = "SAME"
V_INSERT = "INSERT"
V_REMOVE = "REMOVE"

# File statuses
FV_VERIFIED = "verified"
FV_NO_EFFECT = "no_effect"
FV_FORMAT = "format"
FV_OUTDATED = "outdated"
FV_ERROR = "error"

_INT_RE = re.compile(r"^[+-]?\d+$")


@dataclass
class FieldIssue:
    """One changed mod field and how it relates to the vanilla data."""

    path: str  # slash-joined property names
    mod_value: str | None
    vanilla_value: str | None
    verdict: str  # OK / FORMAT / MISSING / TEMPLATE / NO-OP
    note: str = ""
    directive: bool = False  # list insertion/removal row, not a value edit
    noop: bool = False  # directive whose target is absent: edit lands on nothing


@dataclass
class FileVerification:
    """Verification verdict for one mod file."""

    mod_name: str
    entry: str  # path inside the zip
    rel: str  # game-relative path
    pak: str | None
    stored: str | None
    status: str = FV_VERIFIED
    fields: list[FieldIssue] = field(default_factory=list)
    message: str = ""  # extra info (errors, "no effective changes", ...)


def _value_class(value: str | None) -> str:
    if value is None:
        return "none"
    if _INT_RE.match(value):
        return "int"
    try:
        float(value)
        return "float"
    except ValueError:
        return "text"


def _diff_children(mod_props, van_props, path: str, issues: list[FieldIssue],
                   stats: dict | None = None) -> None:
    """Match mod children against vanilla children (keep in sync with merge_mods).

    Pairing: ``_id`` -> ``_index`` (missing = insertion) -> positional; ``_remove`` reports a removal row."""
    van_by_name: dict[str | None, list] = {}
    van_by_id: dict[tuple[str | None, str], object] = {}
    van_by_index: dict[tuple[str | None, str], object] = {}
    for vp in van_props:
        n = vp.get("name")
        van_by_name.setdefault(n, []).append(vp)
        vid = vp.get("_id")
        if vid is not None:
            van_by_id[(n, vid)] = vp
        vidx = vp.get("_index")
        if vidx is not None:
            van_by_index[(n, vidx)] = vp
    used: set[int] = set()
    consumed: dict[str | None, int] = {}

    def pick_positional(name):
        cands = [c for c in van_by_name.get(name, []) if id(c) not in used]
        idx = consumed.get(name, 0)
        consumed[name] = idx + 1
        return cands[idx] if idx < len(cands) else None

    for mp in mod_props:
        name = mp.get("name")
        node_label = path + (name or "?")
        mid = mp.get("_id")
        mindex = mp.get("_index")
        mremove = mp.get("_remove") is not None

        vp = None
        if mid is not None:
            vp = van_by_id.get((name, mid))
            if vp is None:
                issues.append(FieldIssue(
                    node_label, _leaf_value(mp), None, V_MISSING,
                    f"entry keyed _id={mid!r} not found in vanilla",
                ))
                continue
        elif mindex is not None and van_by_index.get((name, mindex)) is not None:
            vp = van_by_index[(name, mindex)]
        elif mindex is not None:
            if mremove:
                # Remove an entry at a position the vanilla list doesn't have.
                issues.append(FieldIssue(
                    node_label,
                    f"remove list entry at position {mindex}",
                    "empty list - nothing to remove",
                    V_NOOP,
                    "no-op: the vanilla list has no entry at this position",
                    directive=True, noop=True,
                ))
                continue
            # No vanilla entry at this position: the mod inserts a new entry.
            sibling_count = len(van_by_name.get(name, []))
            issues.append(FieldIssue(
                node_label,
                f"insert new list entry at position {mindex}",
                f"vanilla list has {sibling_count} entries",
                V_OK,
                "the merge inserts this new entry (subtree handled by merge)",
                directive=True,
            ))
            continue
        elif name is None:
            # Unnamed property (rare): fall back to positional pairing.
            unnamed = [c for c in van_props if c.get("name") is None and id(c) not in used]
            if not unnamed:
                issues.append(FieldIssue(node_label, None, None, V_MISSING,
                                         "unnamed property has no vanilla counterpart"))
                continue
            vp = unnamed[0]
        else:
            vp = pick_positional(name)
            if vp is None:
                if mremove:
                    issues.append(FieldIssue(
                        node_label,
                        "remove this list entry",
                        "empty list - nothing to remove",
                        V_NOOP,
                        "no-op: the vanilla list has no entries",
                        directive=True, noop=True,
                    ))
                else:
                    issues.append(FieldIssue(node_label, _leaf_value(mp), None,
                                             V_MISSING,
                                             "field not present in the current game build"))
                continue

        used.add(id(vp))
        if mremove:
            issues.append(FieldIssue(
                node_label,
                "remove this list entry",
                "entry present in vanilla",
                V_OK,
                "the merge deletes this entry from the list",
                directive=True,
            ))
            continue
        _pair(mp, vp, node_label, issues, stats)


def _pair(mp, vp, node_path: str, issues: list[FieldIssue],
          stats: dict | None = None) -> None:
    """Diff one paired (mod, vanilla) property pair."""
    mod_children = list(mp.findall("Property"))
    van_children = list(vp.findall("Property"))
    if mod_children:
        # Directives against an empty vanilla list simply recurse: each child
        # then reports its own intent (e.g. "remove - empty list, no-op").
        _diff_children(mod_children, van_children, node_path + "/", issues, stats)
    elif van_children:
        issues.append(FieldIssue(node_path, _leaf_value(mp), None,
                                 V_MISSING, "vanilla nests children here, mod is a flat leaf"))
    else:
        mod_value = mp.get("value")
        van_value = vp.get("value")
        if mod_value == van_value:
            if stats is not None:
                stats["same"] = stats.get("same", 0) + 1
            return  # no-op change, not worth reporting
        verdict = V_OK if _value_class(mod_value) == _value_class(van_value) else V_FORMAT
        note = "" if verdict == V_OK else (
            f"mod writes {_value_class(mod_value)} format, "
            f"vanilla field is {_value_class(van_value)}"
        )
        issues.append(FieldIssue(node_path, mod_value, van_value, verdict, note))


def _leaf_value(prop) -> str | None:
    return prop.get("value")


@dataclass
class EditorRow(FieldIssue):
    """One editor field-table row: FieldIssue plus the mod/vanilla Elements."""

    mod_elem: object = None  # the mod-side Element (edit target)
    van_elem: object = None  # the vanilla-side Element (reference)
    can_edit: bool = True  # False for directive/missing rows


def iter_field_rows(mod_root, van_root) -> list[EditorRow]:
    """Every mod field paired against vanilla (unchanged ones too, for the editor)."""
    rows: list[EditorRow] = []
    _iter_children(list(mod_root.findall("Property")),
                   list(van_root.findall("Property")), "", rows)
    return rows


def _iter_children(mod_props, van_props, path: str, rows: list[EditorRow]) -> None:
    van_by_name: dict[str | None, list] = {}
    van_by_id: dict[tuple[str | None, str], object] = {}
    van_by_index: dict[tuple[str | None, str], object] = {}
    for vp in van_props:
        n = vp.get("name")
        van_by_name.setdefault(n, []).append(vp)
        vid = vp.get("_id")
        if vid is not None:
            van_by_id[(n, vid)] = vp
        vidx = vp.get("_index")
        if vidx is not None:
            van_by_index[(n, vidx)] = vp
    used: set[int] = set()
    consumed: dict[str | None, int] = {}

    def pick_positional(name):
        cands = [c for c in van_by_name.get(name, []) if id(c) not in used]
        idx = consumed.get(name, 0)
        consumed[name] = idx + 1
        return cands[idx] if idx < len(cands) else None

    def add(mp, vp, verdict, note, mod_value=None, van_value=None,
            can_edit=True):
        label = path + (mp.get("name") or "?")
        rows.append(EditorRow(
            label, mod_value, van_value, verdict, note,
            mod_elem=mp, van_elem=vp, can_edit=can_edit,
        ))

    for mp in mod_props:
        name = mp.get("name")
        mid = mp.get("_id")
        mindex = mp.get("_index")
        mremove = mp.get("_remove") is not None

        if mid is not None:
            vp = van_by_id.get((name, mid))
            if vp is None:
                add(mp, None, V_MISSING,
                    f"entry keyed _id={mid!r} not found in vanilla",
                    _leaf_value(mp), None, can_edit=False)
                continue
        elif mindex is not None and van_by_index.get((name, mindex)) is not None:
            vp = van_by_index[(name, mindex)]
        elif mindex is not None:
            if mremove:
                add(mp, None, V_NOOP,
                    "no-op: the vanilla list has no entry at this position",
                    f"remove list entry at position {mindex}",
                    "empty list - nothing to remove", can_edit=False)
                continue
            add(mp, None, V_INSERT,
                "the merge inserts this new entry (subtree handled by merge)",
                f"insert new list entry at position {mindex}",
                f"vanilla list has {len(van_by_name.get(name, []))} entries",
                can_edit=False)
            continue
        elif name is None:
            unnamed = [c for c in van_props if c.get("name") is None and id(c) not in used]
            if not unnamed:
                add(mp, None, V_MISSING,
                    "unnamed property has no vanilla counterpart",
                    can_edit=False)
                continue
            vp = unnamed[0]
        else:
            vp = pick_positional(name)
            if vp is None:
                if mremove:
                    add(mp, None, V_NOOP,
                        "no-op: the vanilla list has no entries",
                        "remove this list entry",
                        "empty list - nothing to remove", can_edit=False)
                else:
                    add(mp, None, V_MISSING,
                        "field not present in the current game build",
                        _leaf_value(mp), None, can_edit=False)
                continue

        used.add(id(vp))
        if mremove:
            add(mp, vp, V_REMOVE,
                "the merge deletes this entry from the list",
                "remove this list entry", "entry present in vanilla",
                can_edit=False)
            continue
        mod_children = list(mp.findall("Property"))
        van_children = list(vp.findall("Property"))
        if mod_children or van_children:
            if mod_children and not van_children:
                # Directives against an empty vanilla list recurse (same as
                # _diff_children); a real child edit reports as MISSING below.
                _iter_children(mod_children, van_children, path + name + "/", rows)
                continue
            if van_children and not mod_children:
                add(mp, vp, V_MISSING,
                    "vanilla nests children here, mod is a flat leaf",
                    _leaf_value(mp), None, can_edit=False)
                continue
            _iter_children(mod_children, van_children, path + name + "/", rows)
            continue
        mod_value = mp.get("value")
        van_value = vp.get("value")
        if mod_value == van_value:
            add(mp, vp, V_SAME, "unchanged - equals vanilla", mod_value, van_value)
        else:
            verdict = V_OK if _value_class(mod_value) == _value_class(van_value) else V_FORMAT
            note = "" if verdict == V_OK else (
                f"mod writes {_value_class(mod_value)} format, "
                f"vanilla field is {_value_class(van_value)}"
            )
            add(mp, vp, verdict, note, mod_value, van_value)


def is_raw_asset(entry: str) -> bool:
    """True for non-MBIN/XML files (textures, shaders...): byte-compared, no field editor."""
    return not entry.lower().endswith((".mbin", ".exml", ".mxml"))


def _extract_and_decompile(pcbanks: str, pak: str, stored: str, index: dict, log, compiler: dict) -> Path:
    """Extract one file from its pak and decompile it to MXML (cached)."""
    key = hashlib.md5(
        f"{pak}|{stored}|{index.get('generated')}".encode()
    ).hexdigest()[:16]
    cache_dir = CACHE_DIR / key
    mxml = cache_dir / "vanilla.MXML"
    if mxml.exists():
        return mxml
    cache_dir.mkdir(parents=True, exist_ok=True)

    mbin = cache_dir / "vanilla.MBIN"
    if not mbin.exists():
        with paklib.Pak(Path(pcbanks) / pak) as pk:
            mbin.write_bytes(pk.read_file(stored))

    mbincompiler.decompile(mbin, compiler=compiler)
    log(f"decompiled vanilla {stored} ({pak})")
    return mxml


def _parse_mod_tree(entry: str, data: bytes, work_dir: Path, log) -> ET.Element:
    """Return the mod file's property tree root (Data element)."""
    if entry.lower().endswith((".exml", ".mxml")):
        text = data.decode("utf-8")
        # Mod list-edit convention: valueless flag attributes like `_remove`
        # are not well-formed XML - give them empty values so the parser
        # accepts them.
        text = re.sub(r"(_remove)(?=\s*/?>)", r'\1=""', text)
        return ET.fromstring(text)
    # Shipped MBIN: decompile to MXML in a per-entry work dir.
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = hashlib.md5(entry.encode()).hexdigest()[:12]
    mbin = work_dir / f"{stem}.MBIN"
    mxml = work_dir / f"{stem}.MXML"
    mbin.write_bytes(data)
    if not mxml.exists():
        mbincompiler.decompile(mbin)
    return ET.parse(mxml).getroot()


def _read_mod_data(mod, entry: str) -> bytes:
    """Read one file from a mod (zip archive or loose folder root)."""
    path = Path(mod.path)
    if path.is_dir():
        return (path / entry).read_bytes()
    return ZipFile(path).read(entry)


def _duplicate_groups(mod_root) -> dict[int, int]:
    """Map id(element) -> group size for same-name siblings repeated in one parent.

    Legitimate list entries (with ``_index``/``_id`` attributes) are not flagged."""
    groups: dict[int, int] = {}
    for parent in mod_root.iter():
        by_name: dict[str, list] = {}
        for child in parent:
            if child.tag != "Property" or not child.get("name"):
                continue
            by_name.setdefault(child.get("name"), []).append(child)
        for elems in by_name.values():
            if len(elems) < 2:
                continue
            if all(e.get("_index") is not None for e in elems):
                continue  # positional list entries
            if all(e.get("_id") is not None for e in elems):
                continue  # keyed list entries
            for e in elems:
                groups[id(e)] = len(elems)
    return groups


def _duplicate_fields_note(mod_root) -> str:
    """Human-readable note naming same-name sibling properties repeated
    within one parent (duplicated struct fields from hand edits)."""
    if not _duplicate_groups(mod_root):
        return ""
    flagged: list[str] = []
    seen: set[int] = set()
    for parent in mod_root.iter():
        by_name: dict[str, list] = {}
        for child in parent:
            if child.tag != "Property" or not child.get("name"):
                continue
            by_name.setdefault(child.get("name"), []).append(child)
        for name, elems in by_name.items():
            if len(elems) < 2 or id(elems[0]) in seen:
                continue
            if all(e.get("_index") is not None for e in elems):
                continue
            if all(e.get("_id") is not None for e in elems):
                continue
            seen.add(id(elems[0]))
            flagged.append(f"{name} ×{len(elems)}")
    if not flagged:
        return ""
    return "note: duplicated field(s) in this file: " + ", ".join(flagged)


def verify_scan(scan: dict, index: dict, log, only_mod: str | None = None) -> dict:
    """Verify every resolved mod file; ``only_mod`` restricts to one mod (recompute totals!).

    Returns {"verifications": {(mod, entry): FileVerification}, "counts": {...}}."""
    pcbanks = index["pcbanks"]
    compiler = mbincompiler.ensure_compiler()
    verifications: dict[tuple[str, str], FileVerification] = {}
    counts = {FV_VERIFIED: 0, FV_NO_EFFECT: 0, FV_FORMAT: 0, FV_OUTDATED: 0, FV_ERROR: 0}

    work_dir = CACHE_DIR / "_modwork"
    last_yield = time.monotonic()
    for mod in scan["mods"]:
        if only_mod is not None and mod.name != only_mod:
            continue
        for hit in mod.game_files:
            if not (hit.pak and hit.stored):
                continue  # unresolved: nothing to verify against
            ver = FileVerification(
                mod_name=mod.name, entry=hit.entry, rel=hit.rel,
                pak=hit.pak, stored=hit.stored,
            )
            try:
                if time.monotonic() - last_yield > 0.025:
                    time.sleep(0)
                    last_yield = time.monotonic()
                mod_data = _read_mod_data(mod, hit.entry)
                if is_raw_asset(hit.entry):
                    # Raw asset override (textures, shaders, audio, ...): no
                    # property tree to inspect - a byte difference IS the
                    # change, so verify by comparing against the vanilla
                    # payload.
                    with paklib.Pak(Path(pcbanks) / hit.pak) as pk:
                        van_bytes = pk.read_file(hit.stored)
                    if mod_data == van_bytes:
                        ver.status = FV_NO_EFFECT
                        ver.message = "raw asset equals vanilla bytes"
                    else:
                        ver.status = FV_VERIFIED
                        ver.message = "raw asset override (byte-compared to vanilla)"
                    counts[ver.status] += 1
                    verifications[(mod.name, hit.entry)] = ver
                    continue

                van_mxml = _extract_and_decompile(pcbanks, hit.pak, hit.stored, index, log, compiler)
                van_tree = ET.parse(van_mxml).getroot()
                mod_root = _parse_mod_tree(hit.entry, mod_data, work_dir, log)

                mod_template = mod_root.get("template") if mod_root.tag == "Data" else None
                van_template = van_tree.get("template") if van_tree.tag == "Data" else None
                if mod_template != van_template:
                    ver.status = FV_ERROR
                    ver.message = f"template mismatch: mod={mod_template} vanilla={van_template}"
                    ver.fields = [FieldIssue("<template>", mod_template, van_template, V_TEMPLATE)]
                else:
                    issues: list[FieldIssue] = []
                    stats: dict = {"same": 0}
                    _diff_children(list(mod_root.findall("Property")),
                                   list(van_tree.findall("Property")), "", issues, stats)
                    ver.fields = issues
                    real_changes = [i for i in issues if not i.noop]
                    if any(i.verdict == V_MISSING for i in issues):
                        ver.status = FV_OUTDATED
                    elif any(i.verdict == V_FORMAT for i in issues):
                        ver.status = FV_FORMAT
                    elif issues and not real_changes:
                        # Every edit is a directive landing on nothing.
                        ver.status = FV_NO_EFFECT
                        ver.message = ("no effect: every edit targets something the "
                                       "current game no longer has")
                    elif not issues:
                        if stats["same"]:
                            ver.status = FV_NO_EFFECT
                            ver.message = "no effective changes (all values equal vanilla)"
                        else:
                            ver.status = FV_VERIFIED
                    else:
                        ver.status = FV_VERIFIED
                        noops = sum(1 for i in issues if i.noop)
                        if noops:
                            ver.message = f"{noops} edit(s) in this file are no-ops"
                    dup_note = _duplicate_fields_note(mod_root)
                    if dup_note:
                        ver.message = (
                            f"{ver.message}; {dup_note}" if ver.message else dup_note
                        )
                counts[ver.status] += 1
            except Exception as exc:  # noqa: BLE001
                ver.status = FV_ERROR
                ver.message = f"{type(exc).__name__}: {exc}"
                counts[ver.status] += 1
            verifications[(mod.name, hit.entry)] = ver

    shutil.rmtree(work_dir, ignore_errors=True)
    log(
        f"Verified {sum(counts.values())} file(s): "
        f"{counts[FV_VERIFIED]} clean, {counts[FV_NO_EFFECT]} no effect, "
        f"{counts[FV_FORMAT]} format warning(s), "
        f"{counts[FV_OUTDATED]} outdated, {counts[FV_ERROR]} error(s)"
    )
    return {"verifications": verifications, "counts": counts}
