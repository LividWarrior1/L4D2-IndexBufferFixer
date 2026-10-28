# Root cause: "Too many indices for index buffer. . tell a programmer (N>32768)"

This document separates what is **verified** (from Valve's public SDK headers and
from the binaries/data files of the installed game), what is **strongly
supported** (consistent with the binaries, the SDK and years of community
evidence, but the relevant engine source is not public), and what is an
**estimate** (the risk model used by the tool).

## 1. Where the message comes from

**Verified.** The exact string lives in the shader API layer, not in the game
code:

| file (`Left 4 Dead 2\bin\`) | contains |
|---|---|
| `shaderapidx9.dll` (897 KB, x86) | `Too many indices for index buffer. . tell a programmer (%d>%d)`, `too many indices ...` (a second code path, `CIndexBufferDx8`), `Too many vertices for vertex buffer. . tell a programmer (%d>%d)`, `CMeshDX8::LockIndexBuffer` |
| `shaderapivk.dll` (Vulkan/DXVK path) | the same three messages |

`%d>%d` is *requested indices* > *capacity of the buffer being locked*.

**Verified.** The capacity is a compile-time constant in Valve's public header
`src/public/materialsystem/imesh.h` (Source SDK 2013):

```cpp
// Internal maximums for sizes. Don't use directly, use IMaterialSystem::GetMaxToRender()
enum
{
	INDEX_BUFFER_SIZE  = 32768,
	DYNAMIC_VERTEX_BUFFER_MEMORY = ( 1024 + 512 ) * 1024,
	DYNAMIC_VERTEX_BUFFER_MEMORY_SMALL = 384 * 1024, // Only allocate this much during map transitions
};
```

and `IMatRenderContext::GetMaxToRender()` is documented in `imaterialsystem.h`
as "the number of vertices + indices we can render using the **dynamic mesh**".
`CMeshBuilder` even asserts `*pMaxIndices <= 32768`.

So the number is:

* **not** a VRAM / RAM limit (more memory changes nothing),
* **not** a polygon limit for models,
* the size, in 16-bit indices, of the **one dynamic index buffer** the
  material system uses for geometry that is generated every frame: decals,
  sprites, beams, particles, debug overlays, and similar.

It is a **per-lock (per draw batch) constraint**: a single
`meshBuilder.Begin(...)` that asks for more than 32768 indices is fatal.
Many smaller batches in one frame are fine - the buffer is simply flushed and
reused ("discard and start over" is the normal path when the buffer is full).

## 2. Why a 100 000-triangle model does *not* crash by itself

**Verified on the installed files.** Model geometry is uploaded once into
*static* vertex/index buffers sized for the model, not through the dynamic
buffer. Proof from Valve's own content: `models/survivors/survivor_gambler.dx90.vtx`
(stock Nick) has a body strip group with **36 741 indices** - already more than
32768 - and it renders fine. The densest add-on in the test install (a custom Nick, 101 641
triangles at LOD 0) renders fine until something *projects onto* it.

So the popular explanation "the model has more than 32k polygons" is wrong as
stated. Polygon count matters only indirectly, through *triangle density*
(section 4).

## 3. What *does* go through the dynamic buffer: decals on models

**Strongly supported.** When a decal hits a studio model (blood splatter,
bullet impact), `studiorender.dll` clips the decal against every triangle of
every mesh it touches and stores the resulting triangles with the model
(`studiorender.dll` contains the per-model bookkeeping strings such as
`Unable to apply decals to model (%s), it has more than 255 unique meshes!`).
At draw time the stored decal triangles of one model are emitted through the
dynamic mesh, **batched per decal material**: all `decals/blood3` fragments on
one survivor are one batch, all `decals/blood5` fragments another, and so on.
That batch is what overflows.

Evidence that this is the mechanism:

* The crash is reproducibly tied to decals landing on dense models (Hunter
  pounces, Witch hits, melee kills next to a survivor, shooting a custom
  infected) and goes away when decals are suppressed on those models with
  `$nodecal 1` - the fix used by many Workshop authors (three of the survivor
  add-ons in the test install already ship with `$nodecal 1` on every material).
* In Garry's Mod the same message is worked around with `r_drawmodeldecals 0`.
* The numbers in the message are always a few times 32768 (34 560, 43 518,
  44 232, 55 386, 59 838 in reported cases), i.e. the size of one decal batch,
  never the size of a whole model.

**Verified** from `left4dead2\pak01_dir.vpk`: which decals land on models.
`scripts/decals_subrect.txt` maps impact types to materials; every entry has a
`$modelmaterial` used when the target is a model:

| event | materials | texture × `$decalscale` | footprint |
|---|---|---|---|
| `Blood` (splatter from kills, claws, pounces) | `decals/blood1..6` | 256 px × 0.15 = **38.4 units** square | ≈ 1 475 u² box |
| `Impact.Flesh` (bullets hitting flesh) | `decals/flesh/blood1..5` | 64 px × 0.1 = **6.4 units** square | ≈ 41 u² |

A single large splat on a limb or torso covers several hundred square units of
surface. Each covered triangle costs 3 indices; clipping adds fragments on top.

## 4. The quantity that matters: triangle density

Indices generated by one decal on material *m* ≈ `3 × (triangles of m under the footprint)`
≈ `3 × min(T_m, ρ_m × A_eff)` where ρ is **triangles per square unit** and
`A_eff` the surface the decal actually covers.

Measured by the tool (LOD 0, reference pose) on the test install:

| model | LOD0 tris | densest large material | ρ (tris / 100 u²) | est. indices per large splat |
|---|---|---|---|---|
| Valve `survivor_gambler` (Nick) | 17 446 | `gambler_head` | 1 249 | ~13 600 |
| Valve `survivor_gambler` body | | `gambler_body` | 198 | ~3 600 |
| Valve `hunter` | 11 701 | `hunter_01` | 254 | ~4 600 |
| custom Nick (cel-shaded OC) | **101 641** | `oc_skin` | **2 416** | **~43 500** (one splat already > 32768) |
| custom Zoey (cel-shaded OC) | 46 291 | `..._boots_bootsc_b` | 3 621 | ~24 200 (two splats) |
| custom Rochelle (XPS port) | 39 107 | `kotone_hair` | 1 428 | ~22 600 (two splats) |
| Workshop "OmniMan - Bill" | 55 644 | `omniman_head` | 1 943 | ~20 800 (author added `$nodecal`) |

Stock survivors are already not far from the edge (a face is small and dense),
which is why **vanilla installs can crash, rarely** - community reports agree.
A 2–12× denser surface turns "rarely" into "every time a Hunter lands".

Consequences:

* **Splitting a mesh into more materials does not help** (density is unchanged;
  it only changes how fragments are grouped).
* **Adding LODs does not help** - decals are applied to the LOD being drawn, and
  the crash happens up close.
* **Lowering density helps** (decimating dense materials), and **suppressing
  decals on dense materials removes the problem entirely** for that material.

## 5. What `$nodecal 1` actually does

**Verified.** `materialsystem.dll` holds the material-flag name table in bit
order; `$nodecal` sits at bit 26, between `$halflambert` (27) and the lower
flags, which is `MATERIAL_VAR_SUPPRESS_DECALS = (1 << 26)` in the public
`imaterial.h`. **Strongly supported:** studiorender skips meshes whose material
has that flag when projecting a decal, so no decal triangles are generated for
them at all - the batch never grows. Cost: that material shows no blood or
bullet marks. Other materials of the same model keep their decals, which is why
the tool can be selective.

## 6. Not every index-buffer error is a decal

Other producers of dynamic geometry can request a large batch: particle
systems (Garry's Mod issue reports mention them), sprites/beams, debug
overlays, and world decals (the engine batches those separately and logs
`R_DrawDecalsAll: overflowing m_aBatches` instead of crashing). The "Too many
**vertices** for vertex buffer" variant is the dynamic *vertex* buffer
(1.5 MB) overflowing. `ibfix diagnose` reports the exact kind, the number
requested, the current map and the models mentioned shortly before the error
so that non-decal cases can be told apart.

## 7. Console variables that exist in this build

**Verified** by string search of `engine.dll`: `r_drawmodeldecals`,
`r_maxmodeldecal`, `r_decals` (default string `2048` follows it),
`r_drawdecals`, `r_decal_overlap_count`, `r_decal_overlap_area`,
`r_decal_cover_count`, `cl_player_max_decal_count`, `r_cleardecals`.
**Unknown** without running the game: their cheat/replicated flags and
defaults on this build. `ibfix probe` writes a cfg that prints `help` for each
of them into `console.log`, and `ibfix diagnose` reads the result back, so
the tool never has to guess.

## 8. The tool's risk model (estimate)

For every mesh/material of every add-on model in the survivor, infected and
weapon folders (props optional):

```
indices_per_large_splat(m) = 3 * min( T_m , ρ_m * 600 )      # 600 u² ≈ what a 38.4u splat covers on a limb/torso
indices_per_flesh_impact(m) = 3 * min( T_m , ρ_m * 41 )
worst(model) = max_m indices_per_large_splat(m)
ratio = worst(model) / worst(Valve model it replaces)        # or the Valve median for its category
```

| level | rule |
|---|---|
| CRITICAL | `worst ≥ 32768` (one splat can overflow) or `ratio ≥ 4` |
| HIGH | `worst ≥ 16384` (two same-material splats) or `ratio ≥ 2` |
| MODERATE | `ratio ≥ 1.3` |
| LOW | otherwise |
| MITIGATED | every dense material already has `$nodecal 1` |

Selective fixing puts `$nodecal 1` on materials with
`indices_per_large_splat ≥ 4096` (1/8 of the buffer, i.e. fewer than eight
same-material splats would overflow). Both constants are command-line options.
The relative test is only applied to survivors and infected, where a
comparable Valve model exists.

Known limitations of the estimate: `A_eff` is a fixed guess (real coverage
depends on where a splat lands and on back-face culling), the per-model decal
cap (`r_maxmodeldecal`) and the engine's overlap-retirement rules are not
modelled, and the numbers are upper bounds for small dense parts (a splat
cannot cover more than the whole part).

## Sources

* Source SDK 2013, `src/public/materialsystem/imesh.h`, `imaterialsystem.h`, `imaterial.h`, `src/public/studio.h`, `optimize.h` - https://github.com/ValveSoftware/source-sdk-2013
* Installed binaries: `bin/shaderapidx9.dll`, `bin/shaderapivk.dll`, `bin/materialsystem.dll`, `bin/studiorender.dll`, `bin/engine.dll` (strings; see `tools/inspect_engine_dlls.py`)
* Installed data: `left4dead2/pak01_dir.vpk` → `scripts/decals_subrect.txt`, `materials/decals/*.vmt`, stock survivor/infected models
* Valve Developer Community: [Too many indices for index buffer](https://developer.valvesoftware.com/wiki/Too_many_indices_for_index_buffer), [$nodecal](https://developer.valvesoftware.com/wiki/$nodecal)
* Steam discussions: ["too many indices" error dialog (2020)](https://steamcommunity.com/app/550/discussions/0/2568689796938078904/), [44232>32768 (2020)](https://steamcommunity.com/app/550/discussions/0/4891437254523881778/), [Let's talk about index buffer crashes (2024)](https://steamcommunity.com/app/550/discussions/0/4702413332606353795/), [Hunter attack makes my game crash (2024)](https://steamcommunity.com/app/550/discussions/0/4764334012739975826), [locked thread](https://steamcommunity.com/app/550/discussions/0/144513524086697471), [Addon Priorities](https://steamcommunity.com/app/550/discussions/0/496881136924549036/), [Addons Priority reversed?](https://steamcommunity.com/app/550/discussions/0/864957183011384335)
* Garry's Mod issue tracker: [#3484](https://github.com/Facepunch/garrysmod-issues/issues/3484), [#1286](https://github.com/Facepunch/garrysmod-issues/issues/1286)
* Prior art: [willychen0146/L4D2_hunter_crash_fix](https://github.com/willychen0146/L4D2_hunter_crash_fix) (adds `$nodecal` to every VMT of one VPK; its README notes it "only highly reduces the chance of crashing")
