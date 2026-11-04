# NMS Pak Inspector

Simple tool that can verify, inspect, and install No Man's Sky mods. This app is built mainly for personal use.

<p align="center">
  <a href="preview.png"><img src="preview.png" width="700"></a>
</p>

## Setup/Usage
1. Have Python 3.10+ and the [.NET 8 Desktop Runtime (x64)](https://dotnet.microsoft.com/en-us/download/dotnet/8.0) (required by MBINCompiler).
2. Download/clone this repo.
3. Run `build_exe.bat` and wait until it's done.
4. Run `NMSPakInspector.exe`.
5. Optional: click `Check for compiler updates` in the menu (it downloads MBINCompiler when a newer release is available (kept in mbincompilers/)).
6. Point the app at your game folder and your mods folder; on first launch it builds a pak index (takes a moment).
7. Poke around and figure out the rest.


## Features
- Lists mods from your mod folder; drag and drop archives or folders onto the window to add them.
- Checks mod files against No Man's Sky's pak data without touching the game files; shows what's broken, outdated, or has no effect anymore.
- Inject mod data straight into the game's pak data, only changing what needs to be rewritten.
- Or standard loose installation into `GAMEDATA\MODS` (mutually exclusive with injection).
- Mod merged ability when multiple mods touch the same file.
- Automatic pre-injection backups with byte-exact revert.
- Per mod inline editor with side-by-side tree comparison with diff highlighting, preview, format auto-fix, raw-asset inspector.
- Basic NexusMods support shows author, description, local and latest release, and mod page link. Needs a personal Nexus API key, set once via the menu.
- Automatically detects game updates (restored vanilla paks): clears injection state, rebuilds the index, and re-verifies everything.

## Notes
- NMS shows a "tampering" warning on launch when vanilla paks are modified (loads them fine).
- Mods with outdated/error files can't be injected or installed until the mod is updated for the current game version.
- This app is provided as-is, do not expect it to be user-friendly or bug-free. It is a personal tool. Use at your own risk.

## Credits & license

Pak format handling based on [HGPAKtool](https://github.com/monkeyman192/HGPAKtool).
MBIN compilation via [MBINCompiler](https://github.com/monkeyman192/MBINCompiler) by [monkeyman192](https://github.com/monkeyman192).

MIT License, Copyright (c) 2026 Kilvoctu — see [LICENSE](LICENSE).
