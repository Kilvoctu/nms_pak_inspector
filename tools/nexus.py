"""Nexus metadata for the Info dialog: filename parsing + API queries. Auth = personal API key."""

import json
import re
import time
import urllib.request
from pathlib import Path

_API_BASE = "https://api.nexusmods.com/v1"
_CACHE_TTL = 60 * 60  # 1 hour, same policy as SingularityMM
_UA = "NMS-Pak-Inspector/1.0 (Windows; personal mod manager)"

# (kind, mod_id) -> (expires, payload)
_cache: dict[tuple[str, int], tuple[float, object]] = {}

# Filename schemes -----------------------------------------------------------

_ISO_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}(-\d{2})?Z$")
_ISO_RESTORE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})(?:-(\d{2}))?Z$")
_EXT_RE = re.compile(r"\.(zip|rar|7z)$", re.IGNORECASE)


def parse_nexus_filename(path: str | Path) -> dict | None:
    """Deduce {name, mod_id, version, uploaded} from a Nexus download filename; None if not one.

    New scheme: ``Name ModID Version YYYY-MM-DDTHH-MMZ Hash.zip``; old: ``Name-ModID-Version-<epoch>.zip``."""
    filename = Path(path).name
    ext = _EXT_RE.search(filename)
    if not ext:
        return None
    base = filename[: ext.start()]
    tokens = base.split()

    # New scheme: Name ModID Version ISO-Timestamp Hash
    iso_index = -1
    for i in range(len(tokens) - 1, -1, -1):
        if _ISO_TOKEN.match(tokens[i]):
            iso_index = i
            break
    if iso_index >= 3:
        if not tokens[iso_index - 2].isdigit():
            return None
        if iso_index + 1 >= len(tokens) or not re.fullmatch(
            r"[A-Za-z0-9]+", tokens[iso_index + 1]
        ):
            return None
        parts = _ISO_RESTORE.match(tokens[iso_index])
        uploaded = None
        if parts:
            stamp = (
                f"{parts.group(1)}T{parts.group(2)}:{parts.group(3)}:"
                f"{parts.group(4) or '00'}Z"
            )
            try:
                from datetime import datetime, timezone

                uploaded = int(
                    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except ValueError:
                uploaded = None
        return {
            "name": " ".join(tokens[: iso_index - 2]),
            "mod_id": int(tokens[iso_index - 2]),
            "version": tokens[iso_index - 1],
            "uploaded": uploaded,
        }

    # Old scheme: Name-ModID-Version(-...)-<epoch>
    parts = base.split("-")
    if len(parts) < 3:
        return None
    if not parts[-1].isdigit():
        return None
    mod_id_index = -1
    for i, part in enumerate(parts[:-1]):
        if part.isdigit():
            mod_id_index = i
            break
    if mod_id_index <= 0:
        return None
    return {
        "name": "-".join(parts[:mod_id_index]),
        "mod_id": int(parts[mod_id_index]),
        "version": ".".join(parts[mod_id_index + 1 : -1]).replace("-", "."),
        "uploaded": None,
    }


# Version comparison (numeric-aware: 5.10 > 5.9) -----------------------------

_NEW_VER = re.compile(r"^([0-9.]+)(.*)$")


def is_newer_version(installed: str, latest: str) -> bool:
    """True when ``latest`` is a strictly newer version than ``installed``."""
    if not installed or not latest:
        return False
    im = _NEW_VER.match(str(installed)) or [None, "", ""]
    lm = _NEW_VER.match(str(latest)) or [None, "", ""]
    ins = [int(p) for p in (im[1] or "0").split(".")]
    lat = [int(p) for p in (lm[1] or "0").split(".")]
    for i in range(max(len(ins), len(lat))):
        a = ins[i] if i < len(ins) else 0
        b = lat[i] if i < len(lat) else 0
        if b > a:
            return True
        if b < a:
            return False
    ins_suffix = im[2] or ""
    lat_suffix = lm[2] or ""
    if not ins_suffix and lat_suffix:
        return True
    if ins_suffix and not lat_suffix:
        return False
    return lat_suffix > ins_suffix


# API queries ----------------------------------------------------------------


def _get(url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url, headers={"apikey": api_key, "User-Agent": _UA}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _cached(kind: str, mod_id: int, api_key: str) -> dict:
    key = (kind, mod_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    payload = _get(f"{_API_BASE}/games/nomanssky/mods/{mod_id}"
                   + ("/files.json" if kind == "files" else ".json"), api_key)
    _cache[key] = (time.monotonic() + _CACHE_TTL, payload)
    return payload


def fetch_mod(mod_id: int, api_key: str) -> dict:
    """Mod metadata: name, summary, author, downloads, endorsements…"""
    return _cached("mod", mod_id, api_key)


def fetch_files(mod_id: int, api_key: str) -> dict:
    """The mod's file list (versions + upload timestamps)."""
    return _cached("files", mod_id, api_key)


def latest_file(files: dict) -> dict | None:
    """The newest non-archived file entry (OLD_VERSION files excluded)."""
    candidates = [
        f for f in files.get("files", [])
        if f.get("category_name") != "OLD_VERSION"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("uploaded_timestamp") or 0)


def validate(api_key: str) -> str | None:
    """The account name behind an API key (None when invalid)."""
    try:
        return _get(f"{_API_BASE}/users/validate.json", api_key).get("name")
    except Exception:  # noqa: BLE001 - invalid key / offline
        return None


def page_url(mod_id: int) -> str:
    return f"https://www.nexusmods.com/nomanssky/mods/{mod_id}"
