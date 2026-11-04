"""Install/uninstall mods into the game's loose-mod loader (GAMEDATA\\MODS); disk-derived state."""

import os
import re
import shutil
from pathlib import Path

from .verify_mods import _read_mod_data

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(mod_name: str) -> str:
    """Filesystem-safe folder name for a mod."""
    cleaned = _SAFE_RE.sub("_", mod_name).strip().strip(".")
    return cleaned or "mod"


def gamedata_mods_dir(pcbanks: str) -> Path:
    """The game's loose-mod folder: GAMEDATA (PCBANKS' parent) / MODS."""
    return Path(pcbanks).parent / "MODS"


def mod_dir(pcbanks: str, mod_name: str) -> Path:
    return gamedata_mods_dir(pcbanks) / safe_name(mod_name)


def installed_names(pcbanks: str) -> set[str]:
    """Folder names currently present in the loose-mod folder."""
    root = gamedata_mods_dir(pcbanks)
    if not root.is_dir():
        return set()
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and any(p.iterdir())
    }


def disablemods_flag() -> Path | None:
    """The game's global 'all loose mods disabled' flag, if locatable."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "HelloGames" / "NMS" / "DefaultUser" / "cache" / "DISABLEMODS"


def loose_mods_disabled() -> bool:
    flag = disablemods_flag()
    return bool(flag and flag.exists())


def install_mod(mod, pcbanks: str, log) -> dict:
    """Copy one mod's game files into GAMEDATA\\MODS\\<mod>\\."""
    dest = mod_dir(pcbanks, mod.name)
    dest.mkdir(parents=True, exist_ok=True)
    if loose_mods_disabled():
        log(
            "WARNING: the game's DISABLEMODS flag is set "
            "(%APPDATA%\\HelloGames\\NMS\\...\\cache\\DISABLEMODS) - "
            "loose mods will not load until it is removed"
        )
    count = 0
    for hit in sorted(mod.game_files, key=lambda h: h.entry):
        data = _read_mod_data(mod, hit.entry)
        target = dest / hit.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if hit.rel.lower().endswith(".mxml"):
            log(f"note: {hit.rel} is .MXML - the loose loader loads .MBIN/.EXML only")
        count += 1
    log(f"Installed {mod.name}: {count} file(s) -> {dest}")
    return {"installed": count, "dest": str(dest)}


def uninstall_mod(mod_name: str, pcbanks: str, log) -> dict:
    """Remove one mod's folder from the loose-mod loader."""
    dest = mod_dir(pcbanks, mod_name)
    if dest.is_dir():
        shutil.rmtree(dest)
        log(f"Removed loose mod {mod_name} ({dest})")
        return {"removed": True}
    log(f"{mod_name} had no loose install")
    return {"removed": False}
