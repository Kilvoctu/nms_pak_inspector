"""Project layout: every well-known path in one place (owned by the app)."""

import os
import subprocess
import sys
from pathlib import Path


def project_root() -> Path:
    """The project folder: the executable's folder when frozen (PyInstaller), else the package parent."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """Folder for all runtime state (gitignored; created on demand)."""
    directory = project_root() / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def app_icon_file() -> Path:
    """The app icon: extracted bundle dir when frozen (onefile), else assets/."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and (Path(meipass) / "icon.ico").exists():
        return Path(meipass) / "icon.ico"
    return project_root() / "assets" / "icon.ico"


def state_file() -> Path:
    """GUI/settings file (QSettings INI)."""
    return state_dir() / "state.ini"


def index_file() -> Path:
    """Static path->pak index built by index_paks."""
    return state_dir() / "pak_index.json"


def cache_dir() -> Path:
    """Cache root (decompiled vanilla MXMLs etc.)."""
    return state_dir() / "cache"


def vanilla_cache_dir() -> Path:
    """Decompiled vanilla MBIN cache, keyed per (pak, path, index build)."""
    return cache_dir() / "vanilla"


def mbincompilers_dir() -> Path:
    """Managed folder for downloaded MBINCompiler exes."""
    return state_dir() / "mbincompilers"


def mods_dir() -> Path:
    """Default folder holding the user's loose-mod zips/folders."""
    return project_root() / "mods"


def verify_overrides_file() -> Path:
    """User's saved verify overrides (FORMAT/no-effect decisions)."""
    return state_dir() / "verify_overrides.json"


def inject_state_file() -> Path:
    """Persisted injection state: which mods are baked into the paks."""
    return state_dir() / "inject_state.json"


def inject_backup_dir() -> Path:
    """Pre-injection snapshots - the only copy of vanilla bytes after a pak is modified."""
    return state_dir() / "inject_backups"


GAME_PACKAGE = "HelloGames.NoMansSky"

# CREATE_NO_WINDOW: no console popup for child processes (powershell, MBINCompiler)
# in the windowed exe; 0 is a no-op on non-Windows.
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def appx_install_location() -> str | None:
    """MS Store install location of the game; slow (PowerShell) - callers cache."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-AppxPackage -Name {GAME_PACKAGE}).InstallLocation",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=_SUBPROCESS_FLAGS,
        )
        location = result.stdout.strip()
        return location or None
    except Exception:  # noqa: BLE001 - no Store install / no powershell
        return None


def find_pcbanks(game_dir: str | Path) -> str | None:
    """Locate GAMEDATA\\PCBANKS under a game install (known layouts, then a shallow search)."""
    game = Path(game_dir)
    if not game.is_dir():
        return None
    candidates = [
        game / "Content" / "GAMEDATA" / "PCBANKS",
        game / "GAMEDATA" / "PCBANKS",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.pak")):
            return str(candidate)
    root_depth = len(game.parts)
    for root, dirs, files in os.walk(game):
        depth = len(Path(root).parts) - root_depth
        if depth >= 4:
            dirs[:] = []  # don't descend deeper
            continue
        if Path(root).name.upper() == "PCBANKS" and any(
            f.lower().endswith(".pak") for f in files
        ):
            return root
    return None
