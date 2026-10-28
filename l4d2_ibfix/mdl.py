"""
Minimal parsers for Source engine studio models: ``.mdl`` (studiohdr_t),
``.dx90.vtx`` (OptimizedModel strip data) and ``.vvd`` (vertex data).

Only what the fixer needs is decoded:

* which materials (VMT paths) a model's meshes use,
* how many triangles each mesh has at LOD 0,
* the surface area of each mesh, so we can estimate triangle *density*
  (triangles per square unit), which is what decides how many indices a
  single projected decal will generate.

Struct layouts come from ``public/studio.h`` and ``public/optimize.h`` in the
Source SDK 2013 (32-bit layouts; L4D2 is a 32-bit game and uses MDL v48).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

MDL_ID = b"IDST"
VVD_ID = b"IDSV"
VTX_VERSION = 7
MAX_NUM_LODS = 8

STUDIOHDR_SIZE = 408
TEXTURE_SIZE = 64
BODYPART_SIZE = 16
MODEL_SIZE = 148
MESH_SIZE = 116
VERTEX_SIZE = 48  # mstudiovertex_t: boneweights(16) + pos(12) + normal(12) + uv(8)

# optimize.h, #pragma pack(1)
VTX_FILEHEADER_SIZE = 36
VTX_BODYPART_SIZE = 8
VTX_MODEL_SIZE = 8
VTX_LOD_SIZE = 12
VTX_MESH_SIZE = 9
VTX_STRIPGROUP_SIZE = 25      # +8 for MDL v49+ (topology indices)
VTX_STRIP_SIZE = 27           # +8 for MDL v49+
VTX_VERTEX_SIZE = 9

STRIP_IS_TRILIST = 0x01
STRIP_IS_TRISTRIP = 0x02

# studiohdr_t flags we care about
STUDIOHDR_FLAGS_STATIC_PROP = 0x00000010


class ModelError(Exception):
    pass


def _cstr(buf: bytes, off: int) -> str:
    if off < 0 or off >= len(buf):
        return ""
    end = buf.find(b"\x00", off)
    if end < 0:
        end = len(buf)
    return buf[off:end].decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------- MDL
@dataclass
class MDLMesh:
    material: int          # index into skin table columns (numskinref)
    numvertices: int
    vertexoffset: int      # relative to the parent model's vertices
    lod_vertices: List[int]


@dataclass
class MDLModel:
    name: str
    nummeshes: int
    numvertices: int
    vertexindex: int       # byte offset into the (fixed-up) vertex block
    meshes: List[MDLMesh] = field(default_factory=list)


@dataclass
class MDLBodyPart:
    name: str
    models: List[MDLModel] = field(default_factory=list)


@dataclass
class MDLInfo:
    path: str
    version: int
    checksum: int
    name: str
    flags: int
    numbones: int
    textures: List[str]            # material names as compiled (e.g. "gambler_body")
    cdtextures: List[str]          # search dirs, e.g. "models/survivors/gambler/"
    skins: List[List[int]]         # [family][skinref] -> texture index
    bodyparts: List[MDLBodyPart]
    include_models: List[str] = field(default_factory=list)
    surfaceprop: str = ""

    @property
    def is_static_prop(self) -> bool:
        return bool(self.flags & STUDIOHDR_FLAGS_STATIC_PROP)

    def texture_indices_for_slot(self, slot: int) -> List[int]:
        """All texture indices any skin family maps this material slot to."""
        out: List[int] = []
        for fam in self.skins:
            if slot < len(fam):
                t = fam[slot]
                if t not in out and 0 <= t < len(self.textures):
                    out.append(t)
        if not out and 0 <= slot < len(self.textures):
            out.append(slot)
        return out

    def candidate_vmt_paths(self, texture_index: int) -> List[str]:
        """
        VMT paths the engine will try, in order: ``materials/<cdtexture>/<name>.vmt``
        for every $cdmaterials directory.  If the texture name already contains
        a directory it is also tried relative to ``materials/``.
        """
        name = self.textures[texture_index].replace("\\", "/").strip("/")
        out: List[str] = []
        for cd in self.cdtextures:
            cd = cd.replace("\\", "/").strip("/")
            p = f"materials/{cd}/{name}.vmt" if cd else f"materials/{name}.vmt"
            p = p.replace("//", "/").lower()
            if p not in out:
                out.append(p)
        if "/" in name:
            p = f"materials/{name}.vmt".lower()
            if p not in out:
                out.append(p)
        if not out:
            out.append(f"materials/{name}.vmt".lower())
        return out


def parse_mdl(data: bytes, path: str = "<mdl>") -> MDLInfo:
    if len(data) < STUDIOHDR_SIZE or data[:4] != MDL_ID:
        raise ModelError(f"{path}: not a studio model (bad IDST header)")
    version, checksum = struct.unpack_from("<ii", data, 4)
    name = data[12:76].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    (flags,) = struct.unpack_from("<i", data, 152)
    numbones, boneindex = struct.unpack_from("<ii", data, 156)
    # offsets per studio.h (32-bit)
    numtextures, textureindex = struct.unpack_from("<ii", data, 204)
    numcdtextures, cdtextureindex = struct.unpack_from("<ii", data, 212)
    numskinref, numskinfamilies, skinindex = struct.unpack_from("<iii", data, 220)
    numbodyparts, bodypartindex = struct.unpack_from("<ii", data, 232)
    (surfacepropindex,) = struct.unpack_from("<i", data, 308)
    numincludemodels, includemodelindex = struct.unpack_from("<ii", data, 336)

    def sane(count: int, off: int, size: int) -> bool:
        return 0 <= count < 100000 and 0 <= off and off + count * size <= len(data)

    textures: List[str] = []
    if sane(numtextures, textureindex, TEXTURE_SIZE):
        for i in range(numtextures):
            base = textureindex + i * TEXTURE_SIZE
            (sznameindex,) = struct.unpack_from("<i", data, base)
            textures.append(_cstr(data, base + sznameindex))

    cdtextures: List[str] = []
    if sane(numcdtextures, cdtextureindex, 4):
        for i in range(numcdtextures):
            (off,) = struct.unpack_from("<i", data, cdtextureindex + i * 4)
            cdtextures.append(_cstr(data, off))

    skins: List[List[int]] = []
    if sane(numskinref * numskinfamilies, skinindex, 2) and numskinref > 0:
        for fam in range(numskinfamilies):
            row = list(struct.unpack_from(f"<{numskinref}h", data, skinindex + fam * numskinref * 2))
            skins.append(row)

    bodyparts: List[MDLBodyPart] = []
    if sane(numbodyparts, bodypartindex, BODYPART_SIZE):
        for b in range(numbodyparts):
            bbase = bodypartindex + b * BODYPART_SIZE
            sznameindex, nummodels, base_, modelindex = struct.unpack_from("<iiii", data, bbase)
            bp = MDLBodyPart(_cstr(data, bbase + sznameindex))
            for m in range(max(0, min(nummodels, 4096))):
                mbase = bbase + modelindex + m * MODEL_SIZE
                if mbase + MODEL_SIZE > len(data):
                    break
                mname = data[mbase:mbase + 64].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                _type, _radius, nummeshes, meshindex, numvertices, vertexindex = struct.unpack_from(
                    "<ifiiii", data, mbase + 64
                )
                model = MDLModel(mname, nummeshes, numvertices, vertexindex)
                for k in range(max(0, min(nummeshes, 4096))):
                    kbase = mbase + meshindex + k * MESH_SIZE
                    if kbase + MESH_SIZE > len(data):
                        break
                    material, _modelindex, mnumverts, mvertoff = struct.unpack_from("<iiii", data, kbase)
                    lod_verts = list(struct.unpack_from("<8i", data, kbase + 36 + 12 + 4))
                    model.meshes.append(MDLMesh(material, mnumverts, mvertoff, lod_verts))
                bp.models.append(model)
            bodyparts.append(bp)

    includes: List[str] = []
    if sane(numincludemodels, includemodelindex, 8):
        for i in range(numincludemodels):
            base = includemodelindex + i * 8
            _label, fname = struct.unpack_from("<ii", data, base)
            s = _cstr(data, base + fname)
            if s:
                includes.append(s)

    return MDLInfo(
        path=path, version=version, checksum=checksum, name=name, flags=flags, numbones=numbones,
        textures=textures, cdtextures=cdtextures, skins=skins, bodyparts=bodyparts,
        include_models=includes, surfaceprop=_cstr(data, surfacepropindex) if surfacepropindex > 0 else "",
    )


# ----------------------------------------------------------------------------- VVD
@dataclass
class VVDInfo:
    checksum: int
    num_lods: int
    lod_vertex_counts: List[int]
    positions: List[Tuple[float, float, float]]   # LOD0 (root) vertex positions, fixups applied


def parse_vvd(data: bytes, path: str = "<vvd>", want_positions: bool = True) -> VVDInfo:
    if len(data) < 64 or data[:4] != VVD_ID:
        raise ModelError(f"{path}: not a VVD file")
    version, checksum, num_lods = struct.unpack_from("<iii", data, 4)
    lod_counts = list(struct.unpack_from("<8i", data, 16))
    num_fixups, fixup_start, vertex_start, tangent_start = struct.unpack_from("<iiii", data, 48)
    positions: List[Tuple[float, float, float]] = []
    if want_positions:
        total = lod_counts[0] if lod_counts else 0
        if num_fixups > 0:
            order: List[Tuple[int, int]] = []
            for i in range(num_fixups):
                lod, src, n = struct.unpack_from("<iii", data, fixup_start + i * 12)
                if lod >= 0:  # root LOD includes every fixup
                    order.append((src, n))
        else:
            order = [(0, total)]
        for src, n in order:
            for k in range(n):
                off = vertex_start + (src + k) * VERTEX_SIZE + 16
                if off + 12 > len(data):
                    break
                positions.append(struct.unpack_from("<fff", data, off))
    return VVDInfo(checksum, num_lods, lod_counts, positions)


# ----------------------------------------------------------------------------- VTX
@dataclass
class VTXStripGroup:
    num_verts: int
    num_indices: int
    triangles: int
    vertex_ids: List[int]       # origMeshVertID per strip-group vertex
    indices: List[int]          # indices into vertex_ids (triangle list)
    flags: int


@dataclass
class VTXMesh:
    strip_groups: List[VTXStripGroup]

    @property
    def triangles(self) -> int:
        return sum(sg.triangles for sg in self.strip_groups)


@dataclass
class VTXInfo:
    checksum: int
    num_lods: int
    # bodyparts[b].models[m].lods[l].meshes[k]
    bodyparts: List[List[List[List[VTXMesh]]]]


def _detect_vtx_layout(data: bytes, bodypart_off: int, num_bodyparts: int) -> Tuple[int, int]:
    """
    Return (strip_group_header_size, strip_header_size).

    Walk LOD0 of the first model with both candidate layouts and keep the one
    whose strip-group offsets all land inside the file with sane strip flags.
    """
    n = len(data)

    def violations(sg_size: int, strip_size: int) -> int:
        bad = 0
        checked = 0
        for b in range(max(0, min(num_bodyparts, 64))):
            bbase = bodypart_off + b * VTX_BODYPART_SIZE
            if bbase + VTX_BODYPART_SIZE > n:
                return 10 ** 6
            num_models, model_off = struct.unpack_from("<ii", data, bbase)
            for m in range(max(0, min(num_models, 64))):
                mbase = bbase + model_off + m * VTX_MODEL_SIZE
                if mbase + VTX_MODEL_SIZE > n:
                    return 10 ** 6
                n_lods, lod_off = struct.unpack_from("<ii", data, mbase)
                if n_lods <= 0:
                    continue
                lbase = mbase + lod_off
                if lbase + VTX_LOD_SIZE > n:
                    return 10 ** 6
                num_meshes, mesh_off, _sw = struct.unpack_from("<iif", data, lbase)
                for k in range(max(0, min(num_meshes, 256))):
                    kbase = lbase + mesh_off + k * VTX_MESH_SIZE
                    if kbase + VTX_MESH_SIZE > n:
                        return 10 ** 6
                    num_sg, sg_off, _f = struct.unpack_from("<iiB", data, kbase)
                    for g in range(max(0, min(num_sg, 256))):
                        gbase = kbase + sg_off + g * sg_size
                        if gbase + sg_size > n:
                            bad += 1
                            continue
                        nverts, vert_off, nidx, idx_off, nstrips, strip_off, gflags = struct.unpack_from("<iiiiiiB", data, gbase)
                        checked += 1
                        if nverts < 0 or nidx < 0 or nstrips < 0 or nstrips > 65536:
                            bad += 1
                            continue
                        if gbase + vert_off + nverts * VTX_VERTEX_SIZE > n or gbase + idx_off + nidx * 2 > n:
                            bad += 1
                            continue
                        for s in range(min(nstrips, 8)):
                            sbase = gbase + strip_off + s * strip_size
                            if sbase + strip_size > n:
                                bad += 1
                                break
                            s_nidx, _a, s_nv, _b2, _nb, sflags = struct.unpack_from("<iiiihB", data, sbase)
                            if s_nidx < 0 or s_nidx > nidx or s_nv < 0 or s_nv > nverts or not (sflags & 0x03):
                                bad += 1
                                break
                if checked:
                    return bad
        return bad

    v25 = violations(VTX_STRIPGROUP_SIZE, VTX_STRIP_SIZE)
    if v25 == 0:
        return VTX_STRIPGROUP_SIZE, VTX_STRIP_SIZE
    v33 = violations(VTX_STRIPGROUP_SIZE + 8, VTX_STRIP_SIZE + 8)
    if v33 < v25:
        return VTX_STRIPGROUP_SIZE + 8, VTX_STRIP_SIZE + 8
    return VTX_STRIPGROUP_SIZE, VTX_STRIP_SIZE


def parse_vtx(data: bytes, path: str = "<vtx>", mdl_version: int = 48, want_indices: bool = True) -> VTXInfo:
    if len(data) < VTX_FILEHEADER_SIZE:
        raise ModelError(f"{path}: VTX too small")
    version, _vcache, _mbps, _mbpt, _mbpv, checksum, num_lods, _mrl, num_bodyparts, bodypart_off = struct.unpack_from(
        "<iiHHiiiiii", data, 0
    )
    if version != VTX_VERSION:
        raise ModelError(f"{path}: unsupported VTX version {version}")

    # Some v49 branches (Portal 2 / CS:GO) append two "topology" ints to the
    # strip-group and strip headers; L4D2's own v49 models do NOT.  Detect the
    # layout empirically instead of trusting the MDL version number.
    sg_size, strip_size = _detect_vtx_layout(data, bodypart_off, num_bodyparts)

    def ok(off: int, size: int) -> bool:
        return 0 <= off and off + size <= len(data)

    bodyparts: List[List[List[List[VTXMesh]]]] = []
    for b in range(max(0, min(num_bodyparts, 4096))):
        bbase = bodypart_off + b * VTX_BODYPART_SIZE
        if not ok(bbase, VTX_BODYPART_SIZE):
            break
        num_models, model_off = struct.unpack_from("<ii", data, bbase)
        models: List[List[List[VTXMesh]]] = []
        for m in range(max(0, min(num_models, 4096))):
            mbase = bbase + model_off + m * VTX_MODEL_SIZE
            if not ok(mbase, VTX_MODEL_SIZE):
                break
            n_lods, lod_off = struct.unpack_from("<ii", data, mbase)
            lods: List[List[VTXMesh]] = []
            for l in range(max(0, min(n_lods, MAX_NUM_LODS))):
                lbase = mbase + lod_off + l * VTX_LOD_SIZE
                if not ok(lbase, VTX_LOD_SIZE):
                    break
                num_meshes, mesh_off, _switch = struct.unpack_from("<iif", data, lbase)
                meshes: List[VTXMesh] = []
                for k in range(max(0, min(num_meshes, 4096))):
                    kbase = lbase + mesh_off + k * VTX_MESH_SIZE
                    if not ok(kbase, VTX_MESH_SIZE):
                        break
                    num_sg, sg_off, _mflags = struct.unpack_from("<iiB", data, kbase)
                    groups: List[VTXStripGroup] = []
                    for g in range(max(0, min(num_sg, 4096))):
                        gbase = kbase + sg_off + g * sg_size
                        if not ok(gbase, sg_size):
                            break
                        nverts, vert_off, nidx, idx_off, nstrips, strip_off, gflags = struct.unpack_from(
                            "<iiiiiiB", data, gbase
                        )
                        # triangle count: look at strips (trilist vs tristrip)
                        tris = 0
                        for s in range(max(0, min(nstrips, 65536))):
                            sbase = gbase + strip_off + s * strip_size
                            if not ok(sbase, 19):
                                break
                            s_nidx, _s_ioff, _s_nv, _s_voff, _s_nb, sflags = struct.unpack_from("<iiiihB", data, sbase)
                            if sflags & STRIP_IS_TRISTRIP:
                                tris += max(0, s_nidx - 2)
                            else:
                                tris += s_nidx // 3
                        if nstrips <= 0 or tris == 0:
                            tris = max(0, nidx) // 3
                        vids: List[int] = []
                        idxs: List[int] = []
                        if want_indices:
                            vb = gbase + vert_off
                            if 0 <= nverts <= 1 << 20 and vb + nverts * VTX_VERTEX_SIZE <= len(data):
                                vids = [struct.unpack_from("<H", data, vb + i * VTX_VERTEX_SIZE + 4)[0] for i in range(nverts)]
                            ib = gbase + idx_off
                            if 0 <= nidx <= 1 << 22 and ib + nidx * 2 <= len(data):
                                idxs = list(struct.unpack_from(f"<{nidx}H", data, ib))
                        groups.append(VTXStripGroup(nverts, nidx, tris, vids, idxs, gflags))
                    meshes.append(VTXMesh(groups))
                lods.append(meshes)
            models.append(lods)
        bodyparts.append(models)
    return VTXInfo(checksum, num_lods, bodyparts)


# ----------------------------------------------------------------------- analysis
@dataclass
class MaterialStats:
    slot: int
    texture_names: List[str]
    vmt_candidates: List[str]
    triangles: int = 0
    vertices: int = 0
    area: float = 0.0            # square units at the reference pose (LOD0)

    @property
    def density(self) -> float:
        """Triangles per square unit (0 if area unknown)."""
        return self.triangles / self.area if self.area > 1e-6 else 0.0


@dataclass
class ModelStats:
    path: str
    version: int
    numbones: int
    num_lods: int
    lod0_triangles: int
    lod0_vertices: int
    materials: List[MaterialStats]
    is_static_prop: bool
    checksum_ok: bool
    include_models: List[str]
    bodygroup_count: int
    notes: List[str] = field(default_factory=list)

    @property
    def max_density(self) -> float:
        return max((m.density for m in self.materials), default=0.0)

    @property
    def total_area(self) -> float:
        return sum(m.area for m in self.materials)


def _tri_area(a, b, c) -> float:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def analyze_model(mdl: MDLInfo, vtx: Optional[VTXInfo], vvd: Optional[VVDInfo]) -> ModelStats:
    """
    Combine the three files into per-material LOD0 statistics.

    Every body-group variant (model) of every bodypart is counted, because a
    survivor's default bodygroup selection is unknown here; this is a
    conservative upper bound on what can be on screen at once.
    """
    mats: Dict[int, MaterialStats] = {}
    notes: List[str] = []
    lod0_tris = 0
    checksum_ok = True
    if vtx is not None and vtx.checksum != mdl.checksum:
        checksum_ok = False
        notes.append("VTX checksum does not match MDL (files from different compiles)")
    if vvd is not None and vvd.checksum != mdl.checksum:
        checksum_ok = False
        notes.append("VVD checksum does not match MDL (files from different compiles)")

    positions = vvd.positions if vvd is not None else []
    bodygroups = 0
    for b, bp in enumerate(mdl.bodyparts):
        bodygroups += len(bp.models)
        for m, model in enumerate(bp.models):
            model_vertex_base = model.vertexindex // VERTEX_SIZE if model.vertexindex >= 0 else 0
            vtx_meshes: Sequence[VTXMesh] = []
            if vtx is not None:
                try:
                    vtx_meshes = vtx.bodyparts[b][m][0]  # LOD0
                except IndexError:
                    vtx_meshes = []
            for k, mesh in enumerate(model.meshes):
                slot = mesh.material
                if slot not in mats:
                    tex_idx = mdl.texture_indices_for_slot(slot)
                    names = [mdl.textures[t] for t in tex_idx]
                    cands: List[str] = []
                    for t in tex_idx:
                        for c in mdl.candidate_vmt_paths(t):
                            if c not in cands:
                                cands.append(c)
                    mats[slot] = MaterialStats(slot, names, cands)
                ms = mats[slot]
                ms.vertices += mesh.lod_vertices[0] if mesh.lod_vertices else mesh.numvertices
                if k < len(vtx_meshes):
                    vm = vtx_meshes[k]
                    ms.triangles += vm.triangles
                    lod0_tris += vm.triangles
                    if positions:
                        base = model_vertex_base + mesh.vertexoffset
                        npos = len(positions)
                        for sg in vm.strip_groups:
                            if not sg.indices or not sg.vertex_ids:
                                continue
                            idx = sg.indices
                            vids = sg.vertex_ids
                            nv = len(vids)
                            area = 0.0
                            if sg.flags & STRIP_IS_TRISTRIP and False:
                                pass  # dx90.vtx strips are triangle lists in practice
                            for t in range(0, len(idx) - 2, 3):
                                i0, i1, i2 = idx[t], idx[t + 1], idx[t + 2]
                                if i0 >= nv or i1 >= nv or i2 >= nv:
                                    continue
                                g0, g1, g2 = base + vids[i0], base + vids[i1], base + vids[i2]
                                if g0 >= npos or g1 >= npos or g2 >= npos:
                                    continue
                                area += _tri_area(positions[g0], positions[g1], positions[g2])
                            ms.area += area
    if vtx is None:
        notes.append("no .dx90.vtx found: triangle counts unavailable")
    if vvd is None:
        notes.append("no .vvd found: surface area / density unavailable")
    return ModelStats(
        path=mdl.path, version=mdl.version, numbones=mdl.numbones,
        num_lods=vtx.num_lods if vtx else (vvd.num_lods if vvd else 0),
        lod0_triangles=lod0_tris,
        lod0_vertices=vvd.lod_vertex_counts[0] if vvd and vvd.lod_vertex_counts else 0,
        materials=sorted(mats.values(), key=lambda s: -s.density),
        is_static_prop=mdl.is_static_prop, checksum_ok=checksum_ok,
        include_models=mdl.include_models, bodygroup_count=bodygroups, notes=notes,
    )


def companion_paths(mdl_path: str) -> Tuple[str, str]:
    """('models/x.mdl') -> ('models/x.dx90.vtx', 'models/x.vvd')"""
    stem = mdl_path[:-4] if mdl_path.lower().endswith(".mdl") else mdl_path
    return stem + ".dx90.vtx", stem + ".vvd"
