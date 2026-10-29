# Changelog

## 0.1.1 - 2026-10-29

Fixes #1, "Assumes C Drive at all times and doesn't do anything at all".

- **Game detection rewritten.** 0.1.0 accepted the first folder that merely
  contained a `left4dead2` subfolder, checking the main Steam folder (usually
  C:) first. Steam leaves add-ons and configs behind when a game is moved to
  another drive, so a hollow leftover on C: won over the real install, and
  the scan found nothing and said nothing. Now:
  - libraries with `appmanifest_550.acf` (Steam's record of the install) are
    checked first, on any drive;
  - a folder only counts if `left4dead2\pak01_dir.vpk` exists;
  - libraries are read from both `steamapps\` and `config\libraryfolders.vdf`,
    old and new formats, with Steam's backslash escapes decoded;
  - the fallback scans every fixed drive (A to Z, via the Windows API so
    card readers and optical drives are never touched), not just C to F.
- Every command prints the folder it uses and how it found it; failures list
  every place checked and why each was rejected.
- New `ibfix locate` command.
- `--game-dir` accepts quoted paths (Explorer's *Copy as path*), trailing
  backslashes, sub- and parent folders, or `left4dead2.exe`. cmd.exe's
  `"...\"` quoting trap, which glued the next argument onto the path, is
  repaired.
- `scan` warns when the chosen folder has no add-ons instead of producing an
  empty report.
- `ibfix.bat`: double-clicking shows help and waits instead of flashing shut,
  and a missing Python install is reported.
- Tests: 24 detection tests, including Windows-only ones that run real
  cmd.exe command lines; CI on Windows and Linux.

## 0.1.0 - 2026-10-27

First release.

- VPK v1/v2 reader, v1 writer, streaming re-pack with CRC verification
- MDL / VTX / VVD parsing: per-material triangle count, surface area, density
- Risk model calibrated against Valve's own survivors and special infected
- `scan`, `fix` (replace / overlay / inplace modes, dry-run by default), `status`, `revert`
- `diagnose` (console.log parsing, crash dumps), `probe` (reads decal cvar flags from the game), `bisect`, `inspect`
- Tkinter GUI
- Docs: root cause with evidence, engine-patch feasibility, testing procedure
