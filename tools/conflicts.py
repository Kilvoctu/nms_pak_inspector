"""Field-level conflict detection between mods that share a pak path.

A path collision (two mods shipping the same MBIN) is only yellow - the mods
may edit disjoint fields and merge cleanly. This module inspects the
per-mod verification data (each mod's changed fields) and classifies the
shared edits:

    CONFLICT   - same field, different values: the mods genuinely fight
    DUPLICATE  - same field, identical values: harmless, merge dedupes
    AMBIGUOUS  - repeated-name fields (arrays of same-named properties):
                 positional matching is unreliable, so we flag "possible
                 conflict" rather than guess

Mods whose verification errored (e.g. template mismatch) have unknown field
edits and are excluded from the comparison.
"""

from dataclasses import dataclass, field

K_CONFLICT = "conflict"
K_DUPLICATE = "duplicate"
K_AMBIGUOUS = "ambiguous"


@dataclass
class FieldConflict:
    """One field path edited by multiple mods sharing the same pak path."""

    stored: str
    path: str
    edits: dict[str, str] = field(default_factory=dict)  # mod name -> value
    kind: str = K_CONFLICT


def compute_field_conflicts(scan: dict, verifications: dict) -> list[FieldConflict]:
    """Cross-reference per-mod changed fields on every collided pak path.

    Returns conflicts sorted: real conflicts first, then ambiguous, then
    duplicates."""
    # stored -> field path -> mod name -> [values] (a mod may edit a
    # repeated-name field several times)
    edits: dict[str, dict[str, dict[str, list[str | None]]]] = {}
    for (mod_name, _entry), ver in verifications.items():
        if ver.status == "error":
            continue  # field edits unknown
        for issue in ver.fields:
            if issue.path == "<template>":
                continue
            if getattr(issue, "directive", False) or issue.verdict == "NO-OP":
                continue  # list insert/removal rows: not comparable values
            per_field = edits.setdefault(ver.stored, {})
            per_field.setdefault(issue.path, {}).setdefault(mod_name, []).append(
                issue.mod_value
            )

    out: list[FieldConflict] = []
    for stored, fields in edits.items():
        for path, by_mod in fields.items():
            if len(by_mod) < 2:
                continue  # only one mod edits this field
            repeated = any(len(v) > 1 for v in by_mod.values())
            values = {mod: vals[0] for mod, vals in by_mod.items()}
            if repeated:
                kind = K_AMBIGUOUS
            elif len(set(values.values())) <= 1:
                kind = K_DUPLICATE
            else:
                kind = K_CONFLICT
            out.append(FieldConflict(stored=stored, path=path, edits=values, kind=kind))

    rank = {K_CONFLICT: 0, K_AMBIGUOUS: 1, K_DUPLICATE: 2}
    out.sort(key=lambda c: (rank[c.kind], c.stored, c.path))
    return out
