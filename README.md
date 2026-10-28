# L4D2 Index Buffer Fixer

Finds the add-on models that make Left 4 Dead 2 die with

```
Engine Error: Too many indices for index buffer. . tell a programmer (NNNNN > 32768)
```

and defuses them without deleting the mods: it measures how many decal
triangles each model material can generate, compares that with Valve's own
models, and adds `"$nodecal" "1"` to the dense materials in a **patched copy**
of the add-on. Workshop downloads are never edited; every change is recorded
and reversible with one command.

Pure Python, no third-party packages, Windows 11 / Steam.

* Why the crash happens (with evidence): [docs/ROOT_CAUSE.md](docs/ROOT_CAUSE.md)
* Why the engine limit itself is not worth patching: [docs/ENGINE_PATCH_FEASIBILITY.md](docs/ENGINE_PATCH_FEASIBILITY.md)
* Test procedure and results: [docs/TESTING.md](docs/TESTING.md)

## The short version of the root cause

32768 is `INDEX_BUFFER_SIZE`, the capacity of the engine's single **dynamic**
index buffer (Valve's public `imesh.h`). Models do not render through it; the
decals projected onto them do. When blood lands on a model, the engine clips
the splat against every triangle under it and later draws all fragments of one
decal material on one model as **one batch**. A Valve survivor body has about
2 triangles per square unit; a 38-unit blood splat produces a few thousand
indices. A model with ten times that density produces tens of thousands from a
single splat, and the batch exceeds 32768. Polygon count only matters through
density, which is why "my model is under 32k polys" is no protection and why
`$nodecal` (bit 26, `MATERIAL_VAR_SUPPRESS_DECALS`) on the dense materials is
the fix that works inside the stock engine.

## Install

1. Install Python 3.10 or newer from <https://www.python.org/downloads/windows/>
   (tick *Add python.exe to PATH*; the `py` launcher is installed by default).
2. Download or clone this repository anywhere (it does not need to live in
   the game folder; the game is found through the Steam registry).
3. Open a terminal in the folder (Shift+right-click → *Open in Terminal*) or
   double-click `ibfix-gui.bat`.

No `pip install` is needed. Optionally `pip install .` (or `pipx install .`)
puts an `ibfix` command on your PATH.

## Quick start

```
ibfix scan                 # analyse enabled add-ons, print a risk report
ibfix fix                  # DRY RUN: show exactly which files would be written
ibfix fix --apply          # do it (replace mode)
ibfix status               # what has been applied, and is it still current
ibfix revert --all         # undo everything
```

(`ibfix` is `ibfix.bat`; on other systems use `python ibfix.py ...`.)

## Commands

| command | what it does |
|---|---|
| `scan [--all] [-v] [--include-props] [--threshold N]` | Parses every enabled add-on VPK (`--all`: disabled too), reads each `.mdl/.vtx/.vvd`, computes per-material triangle density and estimated indices per blood splat, compares with the Valve model it replaces, detects materials that already carry `$nodecal`, lists conflicting replacements. Report saved under the data folder. |
| `fix [--apply] [--mode replace\|overlay\|inplace] [--level HIGH] [--all-materials] [--threshold N] [--only ...] [--refresh]` | Builds patched add-ons for everything at `--level` or above (default HIGH+CRITICAL). Without `--apply` it is a dry run. |
| `status` | Lists applied operations; flags ones whose add-on has since been updated (`stale`). |
| `revert [--all \| OPID ...]` | Deletes the generated copies, restores `addonlist.txt` entries and in-place originals. |
| `diagnose` | Reads `left4dead2\console.log` (needs `-condebug`) for index/vertex buffer errors with map and nearby model names, lists crash dumps, shows decal-cvar flags collected by the probe. |
| `probe` | Writes `cfg\ibfix_probe.cfg`; run `exec ibfix_probe` in-game to log the real flags/defaults of `r_drawmodeldecals`, `r_maxmodeldecal`, `cl_player_max_decal_count`, … |
| `bisect start\|good\|bad\|status\|finish` | Halves the enabled add-on set via `addonlist.txt` to find an unknown culprit; `finish` restores the original list. |
| `inspect FILE.vpk` | Analyse any VPK (e.g. your own export before publishing). |

Global options: `--game-dir` (auto-detected from the Steam registry and
library folders), `--data-dir` (default `%LOCALAPPDATA%\L4D2IndexBufferFixer`;
holds reports, backups, cache and `manifest.json`), `--json`.

## Modes

| mode | writes | original | needs load-order assumption |
|---|---|---|---|
| `replace` (default) | `addons\ibfix_<name>.vpk`, a full copy with patched VMTs and ` [IBFix]` appended to the title | set to `0` in `addonlist.txt` | no |
| `overlay` | `addons\ibfix_<name>_overlay.vpk` containing only the patched VMTs (a few KB) | stays enabled | yes: files in `addons\` must win over `addons\workshop\` (reported true; confirm with the `path` console command) |
| `inplace` | rewrites your **local** add-on under the same name | copied to `backups\` first | no (never used for Workshop files) |

## What gets `$nodecal`

Default (*selective*): materials whose estimated indices for one large blood
splat are ≥ 4096 (= fewer than eight same-material splats would overflow).
Clothing with Valve-like density keeps its blood. `--all-materials` suppresses
decals on every material of a flagged model (what most Workshop authors do);
`--threshold` tunes the cut-off. Materials that already have `$nodecal 1` are
left alone, and a model whose dense materials all have it is reported as
`MITIGATED`.

The edit is textual: one line is inserted at the top of the material block
(or into the `insert {}` block of a `patch` material); comments, ordering and
line endings are preserved. Files that cannot be understood are skipped, never
guessed at.

## Safety guarantees

* Dry run by default; `--apply` is required to write anything.
* Workshop VPKs are read-only to the tool. Patched content goes into new files
  under `addons\`.
* `addonlist.txt` is backed up before every modification.
* `manifest.json` records every operation; `revert` undoes them exactly.
* Repacked VPKs are standard v1 archives; every entry's CRC is recomputed, and
  the development test verified all 3058 entries of a 313 MB add-on
  byte-for-byte against the source.
* No engine binaries are touched, no VAC-relevant files are modified; the
  result is ordinary add-on content.

## Limitations (honest ones)

* The risk numbers are **estimates** (see ROOT_CAUSE.md §8). They rank models
  reliably relative to Valve's; they are not a guarantee that a LOW model can
  never crash or that a HIGH one will.
* Not every "Too many indices" is a decal on a model (particles and other
  dynamic geometry can do it). `diagnose` helps tell them apart.
* `overlay` mode depends on add-on load order; `replace` does not.
* If a model's materials live in *another* add-on (shared texture packs), the
  patched VMT is placed in this add-on's copy; the report marks these as
  `other-addon:`.
* Props are only analysed with `--include-props` and are judged on absolute
  cost (no Valve baseline exists for them).

## Project layout

```
ibfix.py                 entry point (python ibfix.py ...)
ibfix.bat / ibfix-gui.bat
l4d2_ibfix/
  vpk.py                 VPK v1/v2 reader, v1 writer, streaming re-pack
  mdl.py                 .mdl / .dx90.vtx / .vvd parsing, per-material tri count, area, density
  keyvalues.py           tolerant KeyValues parser, textual VMT patch, addonlist.txt editing
  gamefiles.py           Steam/L4D2 discovery, add-on enumeration, base-game file access
  analyzer.py            risk model, stock comparison, conflicts, report formatting
  fixer.py               plan/apply/revert, manifest, backups
  diagnostics.py         console.log parsing, probe cfg, crash dumps
  bisect.py              add-on bisection
  cli.py / gui.py        command line / Tkinter front-end
tools/inspect_engine_dlls.py   read-only look at the DLLs holding the limit
tests/test_core.py
docs/
```

## Status

Tested on one real installation (327 add-ons parsed, three high-density survivor
models fixed, a full campaign played crash-free afterwards). Reports from other
installs - especially models the risk model gets wrong in either direction -
are welcome as issues; attach the `scan -v --json` output.

## License

MIT. Not affiliated with Valve.
