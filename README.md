# NMS Pak Inspector

Simple GUI that can verify, inspect, and install No Man's Sky. This app is built mainly for personal use.


## Setup/Usage
1. Have Python 3.10+ and the [.NET 8 Desktop Runtime (x64)](https://dotnet.microsoft.com/en-us/download/dotnet/8.0) (required by MBINCompiler).
2. Download/clone this repo.
3. Run `build_exe.bat` and wait until it's done.
4. Run `NMSPakInspector.exe`.
5. Optional: click **Download compiler** in the menu to fetch MBINCompiler
   (newest release matching your game version, kept in `mbincompilers/`).
6. Point the app at your game folder and your mods folder; on first launch it
   builds a pak index (takes a moment).
7. Hopefully the rest is self-explanatory: pick a mod, check its verification
   status, then **Pak inject** or **Install**.

*Alternatively, run from source: `pip install -r requirements.txt`, then `python main.py`.*

## Features
- Mod list from a mods folder of zips and loose folders; drag and drop
  archives or folders onto the window to add them.
- Verification of every mod file against the real vanilla data: dry-run merge
  of the mod's changed fields onto the decompiled vanilla MBIN, with per-field
  verdicts (OK / FORMAT / MISSING / NO-OP / TEMPLATE) and per-file statuses
  (verified / no-effect / format / outdated / error). Raw assets
  (textures, shaders, ...) are byte-compared instead.
- Pak injection into the vanilla paks with a targeted rewrite: unchanged
  64 KB windows are copied compressed-verbatim, only windows overlapping
  changed bytes are recompressed. Per-mod and group injection (colliding mods
  are merged, field conflicts resolvable).
- Automatic pre-injection backups; byte-exact revert any time.
- Loose install into `GAMEDATA\MODS` as the alternative (mutually exclusive
  with pak injection).
- Editor per mod file: changed/all fields tables, side-by-side compare trees
  with diff highlighting, merged preview, inline value editing with format
  auto-fix, raw-asset inspector (DDS preview).
- Game updates are handled: the app detects restored vanilla paks, clears
  injection state, rebuilds the index, and re-verifies everything.

## Known Issues
- The game shows a "tampering" warning on launch when vanilla paks are
  modified (loads them fine).
- Mods with outdated/error files can't be injected or installed until the mod
  is updated for the current game version.
- Jank.

## Credits & license

Pak format handling based on [HGPAKtool](https://github.com/monkeyman192/HGPAKtool).
MBIN compilation via [MBINCompiler](https://github.com/monkeyman192/MBINCompiler) by [monkeyman192](https://github.com/monkeyman192).

MIT License, Copyright (c) 2026 Kilvoctu — see [LICENSE](LICENSE).
