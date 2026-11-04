"""Apply a mod's file deltas onto the vanilla tree (pairing rules mirror verify_mods._diff_children)."""

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import mbincompiler
from .verify_mods import _value_class


class MergeError(Exception):
    """The mod's edit cannot apply to the current vanilla data."""


def _conform_value(mod_value: str | None, van_value: str | None) -> str | None:
    """Rewrite a changed value in vanilla's numeric style when they differ."""
    mc, vc = _value_class(mod_value), _value_class(van_value)
    if mc == vc:
        return mod_value
    if mc == "float" and vc == "int":
        return str(int(round(float(mod_value))))  # '4320.000000' -> '4320'
    if mc == "int" and vc == "float":
        return f"{float(mod_value):.6f}"  # '72' -> '72.000000'
    return mod_value


def merge_tree(mod_root: ET.Element, van_root: ET.Element) -> tuple[ET.Element, list[str]]:
    """Merge the mod tree onto a copy of the vanilla tree; returns (merged_root, notes)."""
    notes: list[str] = []
    merged = copy.deepcopy(van_root)
    mod_template = mod_root.get("template") if mod_root.tag == "Data" else None
    van_template = merged.get("template") if merged.tag == "Data" else None
    if mod_template and van_template and mod_template != van_template:
        notes.append(
            f"template auto-fixed: mod={mod_template} -> vanilla={van_template}"
        )
    _apply_children(
        list(mod_root.findall("Property")), list(merged.findall("Property")), merged
    )
    if merged_mxml_bytes(merged) == merged_mxml_bytes(van_root):
        notes.append("no effective changes - merged tree equals vanilla")
    return merged, notes


def _apply_children(mod_props, van_props, parent: ET.Element,
                    edits: list | None = None, path: str = "") -> None:
    """Apply the mod's edits onto ``parent``'s vanilla children; with ``edits``, record changes."""
    van_by_name: dict[str | None, list] = {}
    van_by_id: dict[tuple[str | None, str], ET.Element] = {}
    van_by_index: dict[tuple[str | None, str], ET.Element] = {}
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
        mid = mp.get("_id")
        mindex = mp.get("_index")
        mremove = mp.get("_remove") is not None

        if mid is not None:
            vp = van_by_id.get((name, mid))
            if vp is None:
                raise MergeError(f"entry keyed _id={mid!r} not found in vanilla")
        elif mindex is not None and van_by_index.get((name, mindex)) is not None:
            vp = van_by_index[(name, mindex)]
        elif mindex is not None:
            if mremove:
                continue  # removal at a position vanilla doesn't have: no-op
            _insert_entry(parent, mp, name, mindex)
            continue
        elif name is None:
            unnamed = [c for c in van_props if c.get("name") is None and id(c) not in used]
            if not unnamed:
                raise MergeError("unnamed property has no vanilla counterpart")
            vp = unnamed[0]
        else:
            vp = pick_positional(name)
            if vp is None:
                if mremove:
                    continue  # vanilla list has no such entry: no-op
                raise MergeError(f"field {name!r} not present in the current game build")

        used.add(id(vp))
        if mremove:
            parent.remove(vp)
            continue
        _apply_pair(mp, vp, path + (name or "?"), edits)


def _apply_pair(mp: ET.Element, vp: ET.Element, path: str = "?",
                edits: list | None = None) -> None:
    """Apply one paired (mod, vanilla) property pair onto the vanilla copy."""
    mod_children = list(mp.findall("Property"))
    van_children = list(vp.findall("Property"))
    if mod_children:
        # Mirror verify_mods: an empty vanilla list recurses, so directive
        # children land as no-ops; a real child edit raises "not present".
        _apply_children(mod_children, van_children, vp, edits, path + "/")
    elif van_children:
        raise MergeError("vanilla nests children here, mod is a flat leaf")
    else:
        mod_value = mp.get("value")
        van_value = vp.get("value")
        if mod_value != van_value:
            new_value = _conform_value(mod_value, van_value)
            vp.set("value", new_value)
            if edits is not None:
                edits.append((vp, path, van_value, new_value))


@dataclass
class MergeConflict:
    """One field that several merged mods want set to different values."""

    path: str
    choices: list[tuple[int, str]]  # (mod index, conformed value) in order


def merge_many(mod_roots: list, van_root: ET.Element,
               resolutions: dict[str, int] | None = None,
               ) -> tuple[ET.Element, list[str], list[MergeConflict]]:
    """Merge several mods onto one vanilla copy; ``resolutions`` maps field path -> winner."""
    merged = copy.deepcopy(van_root)
    notes: list[str] = []
    for i, mod_root in enumerate(mod_roots):
        mod_template = mod_root.get("template") if mod_root.tag == "Data" else None
        van_template = merged.get("template") if merged.tag == "Data" else None
        if mod_template and van_template and mod_template != van_template:
            notes.append(
                f"mod {i + 1}: template auto-fixed: mod={mod_template} "
                f"-> vanilla={van_template}"
            )
    per_mod_edits: list[list] = []
    for mod_root in mod_roots:
        mod_edits: list = []
        _apply_children(
            list(mod_root.findall("Property")),
            list(merged.findall("Property")),
            merged, mod_edits,
        )
        per_mod_edits.append(mod_edits)

    # Group value edits by element: same element, different values = conflict.
    by_elem: dict[int, list] = {}
    for mod_idx, edits in enumerate(per_mod_edits):
        for elem, path, _old, new in edits:
            by_elem.setdefault(id(elem), []).append((mod_idx, path, new, elem))
    conflicts: list[MergeConflict] = []
    for records in by_elem.values():
        values = {rec[2] for rec in records}
        if len(values) <= 1:
            continue
        path = records[0][1]
        choices = [(rec[0], rec[2]) for rec in records]
        winner = resolutions.get(path) if resolutions else None
        if winner is None:
            winner = choices[0][0]  # first-wins default
        winning_value = next(v for idx, v in choices if idx == winner)
        records[0][3].set("value", winning_value)
        conflicts.append(MergeConflict(path=path, choices=choices))
        notes.append(
            f"conflict on {path}: resolved in favor of mod {winner + 1} "
            f"({winning_value!r})"
        )

    if merged_mxml_bytes(merged) == merged_mxml_bytes(van_root):
        notes.append("no effective changes - merged tree equals vanilla")
    return merged, notes, conflicts


def _insert_entry(parent: ET.Element, entry: ET.Element, name: str | None,
                  position: str) -> None:
    """Copy a mod list entry into the vanilla parent (``_index`` counts same-name siblings)."""
    same = 0
    insert_at = len(parent)
    for i, child in enumerate(parent):
        if child.get("name") == name:
            if same == position:
                insert_at = i
                break
            same += 1
    parent.insert(insert_at, copy.deepcopy(entry))


def merged_mxml_bytes(merged_root: ET.Element) -> bytes:
    """Serialize a merged tree the way MBINCompiler's MXML parser expects."""
    return ET.tostring(merged_root, encoding="utf-8", xml_declaration=True)


def merged_to_mbin(merged_root: ET.Element, work_dir, stem: str) -> bytes:
    """Serialize a merged tree and compile it. Returns the MBIN bytes."""
    mxml = Path(work_dir) / f"{stem}.MXML"
    mxml.write_bytes(merged_mxml_bytes(merged_root))
    mbin = mbincompiler.compile_mxml(mxml)
    data = mbin.read_bytes()
    mbin.unlink()
    mxml.unlink()
    return data


def build_mbin(mod_root: ET.Element, van_root: ET.Element, work_dir, stem: str,
               log) -> bytes:
    """Merge, serialize, and compile. Returns the finished MBIN bytes."""
    merged, notes = merge_tree(mod_root, van_root)
    for note in notes:
        log(f"merge: {note}")
    return merged_to_mbin(merged, work_dir, stem)
