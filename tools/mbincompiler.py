"""MBINCompiler manager: acquire, version, and run the compiler (newest release <= game version)."""

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import repo

MANAGED_DIR = repo.mbincompilers_dir()
INVENTORY = MANAGED_DIR / "inventory.json"
GAME_PACKAGE = repo.GAME_PACKAGE  # single source of truth in repo
REPO_API = "https://api.github.com/repos/monkeyman192/MBINCompiler/releases?per_page=100"
LOG_VERSION_RE = re.compile(r"\[VERSION\]: MBINCompiler v(\d+\.\d+\.\d+(?:\.\d+)?)")

# Release-list cache: GitHub allows only 60 unauthenticated requests/hour,
# so the startup check reuses a recent result instead of re-fetching.
RELEASE_CACHE = repo.cache_dir() / "mbincompiler_releases.json"
RELEASE_CACHE_TTL_S = 6 * 3600  # auto-checks older than this re-fetch


# --- versions -----------------------------------------------------------------


def parse_tag(tag: str) -> tuple[int, ...] | None:
    """'v7.10.0-pre1' -> (7, 10, 0). None if unparseable."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", tag)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


_GAME_VERSION_CACHE: tuple[int, ...] | None = None
_GAME_VERSION_QUERIED = False


def game_version() -> tuple[int, ...] | None:
    """Detected NMS version, e.g. (7, 12, 0, 0); cached (failures cached too)."""
    global _GAME_VERSION_CACHE, _GAME_VERSION_QUERIED
    if _GAME_VERSION_QUERIED:
        return _GAME_VERSION_CACHE
    _GAME_VERSION_QUERIED = True
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-AppxPackage -Name {GAME_PACKAGE}).Version",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=repo._SUBPROCESS_FLAGS,
        )
        version = result.stdout.strip()
        if version:
            _GAME_VERSION_CACHE = tuple(int(p) for p in version.split("."))
    except Exception:  # noqa: BLE001 - no Store install / no powershell
        pass
    return _GAME_VERSION_CACHE


# --- inventory ----------------------------------------------------------------


def _load_inventory() -> dict:
    if INVENTORY.exists():
        return json.loads(INVENTORY.read_text(encoding="utf-8"))
    return {"compilers": []}


def _save_inventory(inv: dict) -> None:
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    INVENTORY.write_text(json.dumps(inv, indent=2), encoding="utf-8")


def _exe_log_version(exe: Path) -> str | None:
    """Self-reported version from the compiler's own log, if it has run."""
    log = exe.with_suffix("").with_suffix(".log")
    log = exe.parent / (exe.stem + ".log")
    if not log.exists():
        return None
    m = LOG_VERSION_RE.search(log.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def local_compilers() -> list[dict]:
    """Downloaded compilers: [{'tag', 'version', 'exe'}...] newest first (stale paths self-heal)."""
    inv = _load_inventory()
    out = []
    for c in inv.get("compilers", []):
        exe = Path(c["exe"])
        if not exe.exists():
            relocated = MANAGED_DIR / exe.name
            if relocated.exists():
                exe = relocated
                c = {**c, "exe": str(relocated)}
        if exe.exists():
            out.append(c)
    out.sort(key=lambda c: parse_tag(c["tag"]) or (0,), reverse=True)
    return out


# --- acquiring ----------------------------------------------------------------


def list_remote_releases(force: bool = False) -> list[dict]:
    """All releases: [{'tag', 'version', 'asset_url'}...] (newest first, disk-cached 6 h)."""
    now = time.time()
    if not force and RELEASE_CACHE.exists():
        try:
            cached = json.loads(RELEASE_CACHE.read_text(encoding="utf-8"))
            if now - cached.get("fetched", 0) < RELEASE_CACHE_TTL_S:
                return [
                    {"tag": r["tag"], "version": tuple(r["version"]),
                     "asset_url": r["asset_url"]}
                    for r in cached["releases"]
                ]
        except Exception:  # noqa: BLE001 - corrupt cache: refetch
            pass
    request = urllib.request.Request(
        REPO_API, headers={"User-Agent": "nms-pack-injector", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.loads(response.read().decode("utf-8"))
    except Exception:
        if RELEASE_CACHE.exists():  # stale but usable (rate limit / offline)
            cached = json.loads(RELEASE_CACHE.read_text(encoding="utf-8"))
            return [
                {"tag": r["tag"], "version": tuple(r["version"]),
                 "asset_url": r["asset_url"]}
                for r in cached["releases"]
            ]
        raise
    out = []
    for rel in releases:
        version = parse_tag(rel.get("tag_name", ""))
        if version is None:
            continue
        asset_url = next(
            (
                a["browser_download_url"]
                for a in rel.get("assets", [])
                if a["name"] == "MBINCompiler.exe"
            ),
            None,
        )
        if asset_url:
            out.append({"tag": rel["tag_name"], "version": version, "asset_url": asset_url})
    try:
        RELEASE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RELEASE_CACHE.write_text(json.dumps({
            "fetched": now,
            "releases": [
                {"tag": r["tag"], "version": list(r["version"]), "asset_url": r["asset_url"]}
                for r in out
            ],
        }, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - cache write is best-effort
        pass
    return out


def pick_release(releases: list[dict], game: tuple[int, ...] | None) -> dict | None:
    """Newest release whose version <= game version (None game: latest)."""
    candidates = [
        r for r in releases if game is None or r["version"] <= game
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r["version"], reverse=True)
    return candidates[0]


def download(release: dict) -> dict:
    """Download a release's MBINCompiler.exe into the managed folder."""
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    exe = MANAGED_DIR / f"MBINCompiler {release['tag']}.exe"
    if not exe.exists():
        request = urllib.request.Request(
            release["asset_url"], headers={"User-Agent": "nms-pack-injector"}
        )
        with urllib.request.urlopen(request, timeout=120) as response, open(exe, "wb") as f:
            f.write(response.read())
    inv = _load_inventory()
    if not any(c["tag"] == release["tag"] for c in inv["compilers"]):
        inv["compilers"].append(
            {"tag": release["tag"], "version": list(release["version"]), "exe": str(exe)}
        )
        _save_inventory(inv)
    return {"tag": release["tag"], "version": release["version"], "exe": str(exe)}


def ensure_compiler(game: tuple[int, ...] | None = None) -> dict:
    """Newest usable compiler: download from the repo if none is good enough."""
    game = game or game_version()
    local = local_compilers()
    if local:
        best = local[0]
        best_ver = tuple(best["version"])
        if game is None or best_ver >= game:
            return best  # local compiler covers the game version
    releases = list_remote_releases()
    release = pick_release(releases, game)
    if release is None:
        if local:
            return local[0]  # nothing suitable remotely; keep what we have
        raise RuntimeError("No MBINCompiler release available")
    # Remote better than local (or nothing local): download it.
    if not local or tuple(local[0]["version"]) < release["version"]:
        return download(release)
    return local[0]


# --- running ------------------------------------------------------------------


def decompile(mbin_path: str | Path, *, compiler: dict | None = None) -> Path:
    """Decompile an MBIN to MXML (written next to the input). Returns the MXML path."""
    mbin = Path(mbin_path)
    mxml = mbin.with_suffix(".MXML")
    if mxml.exists():
        return mxml
    exe = Path((compiler or ensure_compiler())["exe"])
    result = subprocess.run(
        [str(exe), str(mbin)],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=repo._SUBPROCESS_FLAGS,
    )
    if not mxml.exists():
        detail = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(f"MBINCompiler failed on {mbin.name}: {detail.strip()[:400]}")
    return mxml


def compile_mxml(mxml_path: str | Path, *, compiler: dict | None = None) -> Path:
    """Compile an MXML to MBIN (written next to the input). Returns the MBIN path."""
    mxml = Path(mxml_path)
    mbin = mxml.with_suffix(".MBIN")
    if mbin.exists():
        mbin.unlink()
    exe = Path((compiler or ensure_compiler())["exe"])
    result = subprocess.run(
        [str(exe), str(mxml)],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=repo._SUBPROCESS_FLAGS,
    )
    if not mbin.exists():
        detail = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(f"MBINCompiler failed on {mxml.name}: {detail.strip()[:400]}")
    return mbin


if __name__ == "__main__":
    game = game_version()
    print(f"game version: {'.'.join(map(str, game)) if game else 'not detected'}")
    local = local_compilers()
    print(f"local compilers: {[c['tag'] for c in local] or 'none'}")
    print("fetching releases…")
    releases = list_remote_releases()
    print(f"{len(releases)} releases, newest: {releases[0]['tag']}")
    release = pick_release(releases, game)
    print(f"selected: {release['tag']}")
    entry = download(release)
    print(f"downloaded to: {entry['exe']}")
