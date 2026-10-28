# Engine-level options: what is possible, what it costs, and why the tool does not do it

Nothing in this document is applied by the tool. The facts about
the binaries come from a read-only inspection (`tools/inspect_engine_dlls.py`).

## Facts about the installed build

| | |
|---|---|
| Executable / DLLs | 32-bit x86 (`left4dead2.exe`, `bin/*.dll` all `IMAGE_FILE_MACHINE_I386`). There is no 64-bit L4D2 build. |
| Renderers | `shaderapidx9.dll` (Direct3D 9) and `shaderapivk.dll` (Vulkan via `dxvk_d3d9.dll`, the `-vulkan` launch option). **Both contain the same limit and the same error strings.** Switching renderer does not help. |
| Where 32768 appears | `shaderapidx9.dll`: 7 × `push 0x8000`, 9 × `mov eax,0x8000`; `shaderapivk.dll`: same; `materialsystem.dll`: 2 + 11; `studiorender.dll`: 6 × `mov eax,0x8000`; `engine.dll`: 12 + 10 + 8 compares. The constant is baked into **at least four DLLs**, and not every 0x8000 is the index buffer size (it is also a common size and flag value). |
| Index format | 16-bit (`D3DFMT_INDEX16`, the only format `CMeshBuilder` writes). Absolute ceiling for any patch: **65 536 indices**, i.e. at most 2× headroom. |
| Dynamic vertex buffer | 1.5 MB (`DYNAMIC_VERTEX_BUFFER_MEMORY`). Decal vertices carry position, normal, UV and per-vertex data; a batch large enough to need 65k indices is at or over this limit too, so a bigger index buffer would most likely just move the crash to `Too many vertices for vertex buffer`. |

## Option A - patch the constant in the DLLs (32768 → 65535)

**Technically possible, not recommended, not implemented.**

What it would take:

1. Disassemble `shaderapidx9.dll` *and* `shaderapivk.dll` (IDA/Ghidra), find
   `CMeshMgr::Init` (where the dynamic index buffer is created with the
   constant) and `GetMaxIndicesToRender`, plus any inlined copies in
   `materialsystem.dll` and `studiorender.dll` (the 6 sites there are probably
   decal/flex batching limits that must agree with the buffer size).
2. Change every relevant immediate consistently; one missed site produces
   silent corruption instead of an error dialog.
3. Redo it after every game update (offsets move), and remember that Steam's
   *Verify integrity of game files* restores the originals.

Why it is a poor trade even if done right:

* **2× at most.** The densest material in the test install is estimated at ~43 500 indices for a
  single large splat; it would fit, but two splats (87 000) would not. The
  pathological models would still crash, just a little later.
* **The vertex buffer is next** (see table above).
* **Anti-cheat / integrity.** Modified engine DLLs are exactly what VAC and
  server consistency checks look for. Realistically this could only be used
  with `-insecure` on local or private servers, and it is not something to
  recommend to other players.
* It does nothing for the "already dense" models that exist in the Workshop,
  so the material-level fix is still needed.

If you ever want to experiment offline anyway: copy the install, work on the
copy, launch with `-insecure`, keep Steam from updating it, and expect to debug
crashes with no symbols. The tool will not do any of this for you.

## Option B - runtime hook (proxy DLL / injector) that splits oversized batches

The clean fix would be inside `studiorender`'s decal drawing: split one
material's decal fragments into several `meshBuilder.Begin/End` batches of
≤ 32768 indices. That is what a developer with source access would change
(it is a 20-line fix there). Without source it means:

* reverse-engineering an unexported, non-virtual function in
  `studiorender.dll` per build and detouring it;
* a loader that injects into the game process - indistinguishable from cheat
  software to VAC; `-insecure` only.

Not practical as a distributable fix, and it would still have to be redone
after every update. Not implemented.

## Option C - Vulkan (`-vulkan`) or 64-bit

No. Same constant in `shaderapivk.dll`; no 64-bit build exists, and the public
SDK header still says 32768 for the 64-bit branches of other Source games.

## Option D - console variables (engine-provided switches)

These exist in `engine.dll` (verified by string search); whether they are
cheat-protected on this build is what `ibfix probe` + `ibfix diagnose` tell
you from the game's own `help` output:

| cvar | effect if usable | trade-off |
|---|---|---|
| `r_drawmodeldecals 0` | no decals on *any* model: global equivalent of `$nodecal` | no blood/bullet marks on anything, including Valve models |
| `r_maxmodeldecal N` | caps decals stored per model | reduces how many same-material splats can accumulate; cannot stop a *single* oversized splat |
| `cl_player_max_decal_count N` | caps decals on player (survivor) models | same, survivors only |
| `r_decals N` | engine-side total decal budget | affects world decals mostly |
| `r_cleardecals` | clears decals on demand (bind to a key) | manual, world-decal oriented |

If `r_drawmodeldecals` turns out non-cheat, it is the quickest *global*
mitigation; the material-level fix is the *targeted* one that keeps blood on
everything else.

## Option E - server-side plugin (SourceMod) that removes decals from survivors

Only for people who run the server. A plugin that periodically clears decals
on survivor/infected entities reduces accumulation but cannot stop a single
oversized splat, and it does nothing on servers you do not control.

## Option F - material-level suppression (`$nodecal 1`) - **implemented**

* Works with the stock engine, on any server, no integrity concerns (it is
  ordinary add-on content; many Workshop authors already ship it).
* Per-material granularity: only materials whose estimated decal cost is high
  lose decals; clothing with Valve-like density keeps them.
* Fully reversible: the tool writes copies, never edits Workshop files, and
  records every change.

## Option G - fix the models themselves - **recommended for model authors**

If the flagged add-on is your own export, the real fix is upstream:

1. Add `$nodecal 1` to the source VMTs of the dense materials before packing
   (the tool's report lists exactly which).
2. Or lower density where it matters: Valve bodies sit around **2 tris/u²**,
   faces around **12–14 tris/u²**. In the test install the worst skin material
   was at 24 tris/u², lips at 1 700 tris/u², and a cel-shading outline hull
   added 28 558 triangles that also catch decals. Decimating skin/hair/outline
   by 4–8×, or giving the outline hull and tiny trim materials `$nodecal`,
   brings the estimated per-splat cost under the Valve range.
   `ibfix inspect yourfile.vpk` prints per-material density so you can iterate
   until the model reads LOW.
3. LODs do not help this crash (but help performance).

## Verdict

The limit is a hard-coded property of the renderer DLLs, present in both
renderers, bounded by 16-bit indices and by the 1.5 MB dynamic vertex buffer.
Raising it is possible only by patching several engine DLLs, buys at most 2×,
breaks on every update and is incompatible with secure servers. The problem is
better solved where it originates: stop projecting decals onto surfaces whose
triangle density is far above what Valve's own models have.
