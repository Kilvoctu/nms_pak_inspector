# NMS Pak Inspector

Verifies, inspects, and bakes No Man's Sky loose-file mods directly into the
game's vanilla `.pak` archives, so the game loads them natively (indexed,
compressed, bulk reads) instead of parsing loose XML on every launch.

## What it does

- **Scans** a mods folder (zips and loose folders) and resolves every file
  against the game's paks via a header-only pak index.
- **Verifies** every mod file against the real vanilla data: a dry-run merge
  of the mod's changed fields onto the decompiled vanilla MBIN, with
  per-field verdicts (OK / FORMAT / MISSING / NO-OP) and per-file statuses.
  Raw assets (textures, shaders, ...) are byte-compared instead.
- **Injects** mods into the vanilla paks with a targeted rewrite: unchanged
  64 KB windows are copied compressed-verbatim, only windows overlapping
  changed bytes are recompressed. Per-mod and group injection (colliding
  mods are merged, field conflicts resolvable), with pre-injection backups
  and byte-exact revert.
- **Loose install** alternative into `GAMEDATA\MODS` (mutually exclusive
  with pak injection).
- **Editor**: inspect and edit a mod's fields against the full vanilla tree
  (compare trees, merged preview, inline editing, format auto-fix), save back
  to the mod source (EXML rewrite or MBIN recompile via a managed
  MBINCompiler).

## Requirements

- Windows, Python 3.10+
- `pip install -r requirements.txt`
- MBINCompiler (downloaded into the managed `mbincompilers\` folder from the
  app's menu; newest release matching the detected game version)

## Status

Private development repo. Not production-ready for distribution.

