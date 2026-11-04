"""Static crash-risk audit of the currently injected mods (no game boot needed).

Checks each baked file vs its pre-image: MBIN round-trip + template drift, DDS headers, SPIR-V, pak integrity."""

import hashlib
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from . import mbincompiler, paklib, repo
from .inject_mods import BACKUP_DIR, load_state
from .verify_mods import _templates_equivalent

WORK_ROOT = repo.cache_dir() / "_auditwork"

# Paths the game parses before the title screen: crashes here are boot crashes.
BOOT_PREFIXES = ("metadata/reality/", "globals/", "pipelines/", "ui/boot/", "shaders/")

_SEV_ORDER = {"risk": 0, "warn": 1, "info": 2, "ok": 3}


def _boot_critical(stored: str) -> bool:
    return stored.startswith(BOOT_PREFIXES)


def _dds_header(data: bytes) -> dict | None:
    """Parse the fields of a 128-byte DDS header that matter for the game."""
    if len(data) < 128 or data[:4] != b"DDS ":
        return None
    (height, width) = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    four_cc = data[84:88]
    fmt = four_cc.decode("ascii", "replace").strip("\x00 ")
    block = 8 if four_cc in (b"DXT1", b"BC4S", b"BC4U") else 16
    if four_cc == b"DX10":
        if len(data) < 132:
            return None
        dxgi = struct.unpack_from("<I", data, 128)[0]
        fmt = f"DX10/{dxgi}"
        block = {70: 16, 71: 8, 72: 8, 73: 8, 74: 8, 77: 16, 80: 16, 98: 16}.get(dxgi)
        if block is None:
            block = 0  # uncompressed/unknown: size check skipped
    expected = None
    if block:
        expected = 128 + sum(
            max(1, (max(1, width >> m) + 3) // 4) * max(1, (max(1, height >> m) + 3) // 4) * block
            for m in range(max(1, mips))
        )
    return {"w": width, "h": height, "mips": mips, "fmt": fmt, "expected": expected}


def _tree_leaves(root: ET.Element) -> dict[str, str]:
    """Flatten an MXML tree to {path: value} (occurrence-indexed for repeats)."""
    out: dict[str, str] = {}

    def walk(el: ET.Element, prefix: str) -> None:
        name = el.get("name") or ""
        path = f"{prefix}/{name}" if prefix else name
        seen: dict[str, int] = {}
        for child in el:
            cname = child.get("name") or ""
            n = seen.get(cname, 0)
            seen[cname] = n + 1
            walk(child, f"{path}[{n}]" if prefix else cname)
        value = el.get("value")
        if value is not None:
            out[path] = value

    walk(root, "")
    return out


def _decompile_bytes(data: bytes, work: Path, stem: str) -> tuple[ET.Element | None, str]:
    """Decompile an MBIN payload; returns (tree or None, error detail)."""
    work.mkdir(parents=True, exist_ok=True)
    mbin = work / f"{stem}.MBIN"
    mbin.write_bytes(data)
    try:
        mxml = mbincompiler.decompile(mbin)
        return ET.parse(mxml).getroot(), ""
    except Exception as exc:  # noqa: BLE001 - any compiler failure is a finding
        return None, str(exc)[:300]


def _diff_stats(a: dict[str, str], b: dict[str, str]) -> tuple[int, int, int]:
    """Changed / added / removed leaf counts between two flattened trees."""
    changed = sum(1 for k in a.keys() & b.keys() if a[k] != b[k])
    return changed, len(b.keys() - a.keys()), len(a.keys() - b.keys())


def audit_injections(pcbanks: str, log) -> dict:
    """Audit every injected file; returns {"findings": [...], "summary": str}."""
    import shutil

    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    state = load_state()
    findings: list[dict] = []

    def add(mod: str, pak: str, stored: str, sev: str, detail: str) -> None:
        findings.append(
            {"mod": mod, "pak": pak, "file": stored, "severity": sev, "detail": detail}
        )

    if not state:
        return {"findings": [], "summary": "nothing is injected"}

    # Group by pak so each pak is opened once.
    by_pak: dict[str, list[tuple[str, dict]]] = {}
    for mod, entry in state.items():
        for f in entry.get("files", []):
            by_pak.setdefault(f["pak"], []).append((mod, f))

    for pak, items in sorted(by_pak.items()):
        pak_path = Path(pcbanks) / pak
        try:
            pk = paklib.Pak(pak_path)
        except Exception as exc:  # noqa: BLE001 - unreadable pak is top risk
            add("-", pak, "(whole pak)", "risk", f"pak failed to open: {exc}")
            continue
        try:
            for mod, f in sorted(items, key=lambda x: x[1]["stored"]):
                stored = f["stored"]
                pre = BACKUP_DIR / f["backup"]
                try:
                    baked = pk.read_file(stored)
                except Exception as exc:  # noqa: BLE001
                    add(mod, pak, stored, "risk", f"unreadable in pak: {exc}")
                    continue
                pre_bytes = pre.read_bytes() if pre.exists() else None
                if pre_bytes is None:
                    add(mod, pak, stored, "warn", "pre-injection backup missing")
                    continue
                suffix = stored.rsplit(".", 1)[-1].lower()
                stem = hashlib.md5(f"{pak}|{stored}".encode()).hexdigest()[:12]
                if suffix == "mbin":
                    _audit_mbin(mod, pak, stored, baked, pre_bytes, stem, add)
                elif suffix == "dds":
                    _audit_dds(mod, pak, stored, baked, pre_bytes, add)
                elif suffix == "spv":
                    _audit_spv(mod, pak, stored, baked, pre_bytes, add)
                else:
                    if baked == pre_bytes:
                        add(mod, pak, stored, "warn", "baked bytes equal vanilla (no-op bake)")
                    else:
                        add(mod, pak, stored, "ok", "raw override differs from vanilla as expected")
        finally:
            pk.close()

    findings.sort(key=lambda x: (_SEV_ORDER.get(x["severity"], 9), not _boot_critical(x["file"]), x["mod"], x["file"]))
    risks = sum(1 for x in findings if x["severity"] == "risk")
    warns = sum(1 for x in findings if x["severity"] == "warn")
    total = len(findings)
    summary = f"audited {total} injected file(s): {risks} risk, {warns} warn"
    log(summary)
    return {"findings": findings, "summary": summary}


def _audit_mbin(mod: str, pak: str, stored: str, baked: bytes, pre: bytes, stem: str, add) -> None:
    """Round-trip decompile the baked MBIN and compare against its pre-image."""
    tree, err = _decompile_bytes(baked, WORK_ROOT, stem)
    if tree is None:
        add(mod, pak, stored, "risk", f"baked MBIN will not decompile: {err}")
        return
    van_tree, van_err = _decompile_bytes(pre, WORK_ROOT, stem + "v")
    if van_tree is None:
        add(mod, pak, stored, "info", f"vanilla pre-image will not decompile (compiler limitation): {van_err[:160]}")
        return
    tpl = tree.get("template") or ""
    van_tpl = van_tree.get("template") or ""
    if tpl != van_tpl and not _templates_equivalent(tpl, van_tpl):
        add(mod, pak, stored, "warn", f"template drift: baked={tpl} vanilla={van_tpl}")
    changed, added, removed = _diff_stats(_tree_leaves(van_tree), _tree_leaves(tree))
    if changed + added + removed == 0:
        add(mod, pak, stored, "warn", "no-op bake (baked MBIN equals vanilla fields)")
    else:
        add(
            mod, pak, stored, "ok",
            f"decompiles cleanly; {changed} changed, {added} added, {removed} removed field(s) vs vanilla",
        )


def _audit_spv(mod: str, pak: str, stored: str, baked: bytes, pre: bytes, add) -> None:
    """SPIR-V magic + version drift vs the vanilla pre-image."""
    if baked[:4] != b"\x03\x02\x23\x07":
        add(mod, pak, stored, "risk", "not a SPIR-V module (bad magic)")
        return
    if pre[:4] != b"\x03\x02\x23\x07":
        add(mod, pak, stored, "info", "valid SPIR-V; vanilla pre-image is not SPIR-V")
        return
    bv, pv = baked[4:8], pre[4:8]
    if bv != pv:
        add(
            mod, pak, stored, "warn",
            f"SPIR-V version differs from vanilla: {bv.hex()} vs {pv.hex()} "
            "(shader built for a different game build?)",
        )
    else:
        add(mod, pak, stored, "ok", "SPIR-V magic and version match vanilla")


def _audit_dds(mod: str, pak: str, stored: str, baked: bytes, pre: bytes, add) -> None:
    """Compare baked DDS header sanity against the vanilla pre-image."""
    hdr = _dds_header(baked)
    if hdr is None:
        add(mod, pak, stored, "risk", "not a valid DDS (bad magic or truncated header)")
        return
    if hdr["expected"] is not None and len(baked) < hdr["expected"]:
        add(
            mod, pak, stored, "risk",
            f"DDS payload truncated: header declares ~{hdr['expected']:,} bytes, file has {len(baked):,}",
        )
        return
    van = _dds_header(pre)
    if van is None:
        add(mod, pak, stored, "info", "baked DDS parses; vanilla pre-image is not a plain DDS")
        return
    if hdr["fmt"] != van["fmt"]:
        add(
            mod, pak, stored, "info",
            f"pixel format differs from vanilla: {hdr['fmt']} vs {van['fmt']} "
            "(valid DDS - the loader accepts both, but watch for artifacts)",
        )
    elif (hdr["w"], hdr["h"]) != (van["w"], van["h"]):
        add(mod, pak, stored, "warn", f"dimensions differ from vanilla: {hdr['w']}x{hdr['h']} vs {van['w']}x{van['h']}")
    else:
        add(mod, pak, stored, "ok", f"DDS header matches vanilla format {hdr['fmt']} {hdr['w']}x{hdr['h']}")
