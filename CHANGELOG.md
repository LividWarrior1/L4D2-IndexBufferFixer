# Changelog

## 0.1.0 - 2026-10-27

First release.

- VPK v1/v2 reader, v1 writer, streaming re-pack with CRC verification
- MDL / VTX / VVD parsing: per-material triangle count, surface area, density
- Risk model calibrated against Valve's own survivors and special infected
- `scan`, `fix` (replace / overlay / inplace modes, dry-run by default), `status`, `revert`
- `diagnose` (console.log parsing, crash dumps), `probe` (reads decal cvar flags from the game), `bisect`, `inspect`
- Tkinter GUI
- Docs: root cause with evidence, engine-patch feasibility, testing procedure
