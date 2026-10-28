"""
Risk analysis: which add-on models are likely to overflow the engine's
32768-index dynamic index buffer when decals are projected onto them.

The model (see docs/ROOT_CAUSE.md for the derivation)
------------------------------------------------------
When a decal (blood splat, bullet impact) hits a studio model, studiorender
clips the decal against every triangle of every mesh it touches and stores
the resulting triangles.  At draw time **all decals that share a material on
one model are emitted as a single dynamic mesh**, and the dynamic index
buffer holds at most ``INDEX_BUFFER_SIZE = 32768`` indices (3 per triangle).
A single lock larger than that is the fatal
``Too many indices for index buffer. . tell a programmer (N>32768)``.

So the quantity that matters is *triangles under a decal footprint*, i.e.
triangle **density** (triangles per square unit), not the model's total
polygon count.  For each material we estimate:

    indices_per_splat = 3 * min( triangles_m , density_m * A_eff )

with ``A_eff`` the surface area a typical decal actually covers, and compare
against the Valve model the add-on replaces (which is known to be
*mostly* safe).  All numbers are estimates; they are reported as such.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gamefiles import AddonInfo, GameInstall, quick_hash
from .keyvalues import decode_text, find_nodecal
from .mdl import ModelError, ModelStats, analyze_model, companion_paths, parse_mdl, parse_vtx, parse_vvd
from .vpk import VPK, VPKError

INDEX_BUFFER_SIZE = 32768

# Effective surface area (square units) a decal covers on a character.
#   "Blood" splats:   decals/blood1..6, 256px * $decalscale 0.15 = 38.4u square -> ~1475u^2 box,
#                     of which roughly half lands on front-facing surface of a limb/torso.
#   "Impact.Flesh":   decals/flesh/blood1..5, 64px * 0.1 = 6.4u square -> ~41u^2.
A_EFF_BIG_SPLAT = 600.0
A_EFF_SMALL_IMPACT = 41.0

LEVELS = ("MITIGATED", "LOW", "MODERATE", "HIGH", "CRITICAL", "N/A")
LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}

CATEGORY_RULES = (
    ("survivor", re.compile(r"^models/survivors/", re.I)),
    ("infected", re.compile(r"^models/infected/", re.I)),
    # first-person weapon / arm models: anything named v_*.mdl or under weapons/arms
    ("viewmodel", re.compile(r"^models/(weapons/arms/|v_models/|.*/v_[^/]*\.mdl$|weapons/v_)", re.I)),
    ("weapon", re.compile(r"^models/(weapons/|w_)", re.I)),
    ("prop", re.compile(r"^models/props", re.I)),
)
DECAL_CATEGORIES = ("survivor", "infected", "weapon", "prop", "other")
DEFAULT_CATEGORIES = ("survivor", "infected", "weapon")


def categorize(model_path: str) -> str:
    p = model_path.replace("\\", "/").lower()
    for name, rx in CATEGORY_RULES:
        if rx.search(p):
            return name
    return "other"


# ------------------------------------------------------------------ results
@dataclass
class MaterialRisk:
    slot: int
    texture_names: List[str]
    vmt_path: Optional[str]          # resolved path (first candidate that exists)
    vmt_source: str                  # 'addon' | 'other-addon:<name>' | 'base' | 'missing'
    vmt_candidates: List[str]
    triangles: int
    vertices: int
    area: float
    density: float                   # triangles per square unit
    indices_per_big_splat: int
    indices_per_small_impact: int
    splats_to_overflow: float        # same-material large splats before > 32768
    has_nodecal: Optional[bool]      # None if VMT not readable
    recommend_nodecal: bool = False


@dataclass
class ModelRisk:
    path: str
    category: str
    lod0_triangles: int
    lod0_vertices: int
    num_lods: int
    numbones: int
    bodygroups: int
    worst_indices_per_big_splat: int
    stock_path: Optional[str]
    stock_lod0_triangles: Optional[int]
    stock_worst_indices_per_big_splat: Optional[int]
    ratio_vs_stock: Optional[float]
    level: str
    reasons: List[str]
    materials: List[MaterialRisk]
    notes: List[str] = field(default_factory=list)
    error: str = ""


@dataclass
class AddonRisk:
    path: str
    entry: str
    title: str
    author: str
    workshop_id: Optional[str]
    is_workshop: bool
    enabled: Optional[bool]
    size: int
    hash: str
    level: str
    models: List[ModelRisk]
    skipped_models: int = 0
    error: str = ""

    @property
    def display_name(self) -> str:
        base = self.title or Path(self.path).name
        return f"{base} [{self.workshop_id}]" if self.workshop_id else base


@dataclass
class ScanReport:
    generated: str
    game_dir: str
    addons: List[AddonRisk]
    conflicts: Dict[str, List[str]]          # model path -> [addon display names] (enabled only)
    baselines: Dict[str, Dict[str, float]]   # category -> {worst_indices, lod0_triangles}
    parameters: Dict[str, float]
    warnings: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @staticmethod
    def from_json(text: str) -> "ScanReport":
        d = json.loads(text)
        addons = []
        for a in d["addons"]:
            models = []
            for m in a["models"]:
                mats = [MaterialRisk(**x) for x in m.pop("materials")]
                models.append(ModelRisk(materials=mats, **m))
            a.pop("models")
            addons.append(AddonRisk(models=models, **a))
        d.pop("addons")
        return ScanReport(addons=addons, **d)


# ---------------------------------------------------------------- analysis
def _cost(triangles: int, density: float, area_eff: float) -> int:
    if triangles <= 0:
        return 0
    covered = density * area_eff if density > 0 else triangles
    return int(round(3 * min(triangles, covered)))


def classify(worst: int, ratio: Optional[float], category: str) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    if category == "viewmodel":
        return "N/A", ["first-person view models never receive decals"]
    level = "LOW"
    if worst >= INDEX_BUFFER_SIZE:
        level = "CRITICAL"
        reasons.append(f"a single large blood splat on the densest material can exceed {INDEX_BUFFER_SIZE} indices (~{worst})")
    elif worst >= INDEX_BUFFER_SIZE // 2:
        level = "HIGH"
        reasons.append(f"two same-material blood splats can exceed the buffer (~{worst} indices each)")
    # The relative test only makes sense where a comparable Valve model exists
    # (survivors / infected).  Props, weapons and misc models are judged on the
    # absolute single-splat cost alone.
    if ratio is not None and category in ("survivor", "infected"):
        if ratio >= 4 and LEVEL_ORDER[level] < LEVEL_ORDER["CRITICAL"]:
            level = "CRITICAL"
        elif ratio >= 2 and LEVEL_ORDER[level] < LEVEL_ORDER["HIGH"]:
            level = "HIGH"
        elif ratio >= 1.3 and LEVEL_ORDER[level] < LEVEL_ORDER["MODERATE"]:
            level = "MODERATE"
        if ratio >= 1.3:
            reasons.append(f"generates ~{ratio:.1f}x the decal indices of the Valve model it replaces")
    if not reasons:
        reasons.append("decal cost comparable to Valve's own models")
    return level, reasons


class Analyzer:
    def __init__(self, game: GameInstall, data_dir: Path, categories=DEFAULT_CATEGORIES,
                 selective_threshold: int = 4096, log=print):
        self.game = game
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "cache"
        self.categories = tuple(categories)
        self.selective_threshold = selective_threshold
        self.log = log
        self._stock_cache: Dict[str, Optional[ModelStats]] = {}
        self._baselines: Optional[Dict[str, Dict[str, float]]] = None

    # ------------------------------------------------------------- stock
    def stock_stats(self, model_path: str) -> Optional[ModelStats]:
        key = model_path.lower()
        if key in self._stock_cache:
            return self._stock_cache[key]
        st = None
        data = self.game.read_base_file(model_path)
        if data:
            vtxp, vvdp = companion_paths(model_path)
            try:
                mdl = parse_mdl(data, model_path)
                vtxd = self.game.read_base_file(vtxp)
                vvdd = self.game.read_base_file(vvdp)
                vtx = parse_vtx(vtxd, vtxp, mdl.version) if vtxd else None
                vvd = parse_vvd(vvdd, vvdp) if vvdd else None
                st = analyze_model(mdl, vtx, vvd)
            except (ModelError, Exception):
                st = None
        self._stock_cache[key] = st
        return st

    def baselines(self) -> Dict[str, Dict[str, float]]:
        """Median 'worst indices per splat' of Valve's own survivors / special infected."""
        if self._baselines is not None:
            return self._baselines
        cache = self.cache_dir / "baselines.json"
        if cache.exists():
            try:
                self._baselines = json.loads(cache.read_text())
                return self._baselines
            except Exception:
                pass
        groups = {
            "survivor": [f"models/survivors/survivor_{n}.mdl" for n in
                         ("gambler", "coach", "producer", "mechanic", "namvet", "biker", "teenangst", "manager")],
            "infected": [f"models/infected/{n}.mdl" for n in ("hunter", "smoker", "boomer", "charger", "jockey", "spitter", "witch", "boomette")],
        }
        out: Dict[str, Dict[str, float]] = {}
        for cat, paths in groups.items():
            worsts, tris = [], []
            for p in paths:
                st = self.stock_stats(p)
                if st and st.lod0_triangles:
                    worsts.append(max((_cost(m.triangles, m.density, A_EFF_BIG_SPLAT) for m in st.materials), default=0))
                    tris.append(st.lod0_triangles)
            if worsts:
                worsts.sort(); tris.sort()
                out[cat] = {"worst_indices": float(worsts[len(worsts) // 2]), "lod0_triangles": float(tris[len(tris) // 2])}
        # weapons/props/other: reuse infected numbers (no better reference)
        for cat in ("weapon", "prop", "other"):
            out[cat] = dict(out.get("infected") or out.get("survivor") or {"worst_indices": 5000.0, "lod0_triangles": 10000.0})
        self._baselines = out
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out, indent=2))
        except OSError:
            pass
        return out

    # ------------------------------------------------------------- addons
    def analyze_addon(self, info: AddonInfo, all_addons: List[AddonInfo], use_cache: bool = True) -> AddonRisk:
        h = quick_hash(info.path)
        cache_file = self.cache_dir / f"{info.path.stem}.{h[:16]}.json"
        if use_cache and h and cache_file.exists():
            try:
                d = json.loads(cache_file.read_text())
                models = []
                for m in d["models"]:
                    mats = [MaterialRisk(**x) for x in m.pop("materials")]
                    models.append(ModelRisk(materials=mats, **m))
                d.pop("models")
                d.update(dict(entry=info.entry, title=info.title, author=info.author, enabled=info.enabled))
                return AddonRisk(models=models, **d)
            except Exception:
                pass

        risk = AddonRisk(
            path=str(info.path), entry=info.entry, title=info.title, author=info.author,
            workshop_id=info.workshop_id, is_workshop=info.is_workshop, enabled=info.enabled,
            size=info.size, hash=h, level="LOW", models=[],
        )
        try:
            vpk = VPK(info.path)
        except (VPKError, OSError) as e:
            risk.error = str(e)
            risk.level = "N/A"
            return risk
        with vpk:
            others = [a for a in all_addons if a.path != info.path and a.is_enabled]
            for mdl_path in sorted(vpk.paths_with_suffix(".mdl")):
                cat = categorize(mdl_path)
                if cat not in self.categories and cat != "viewmodel":
                    risk.skipped_models += 1
                    continue
                mr = self._analyze_model(vpk, mdl_path, cat, others)
                if mr is None:
                    risk.skipped_models += 1
                    continue
                risk.models.append(mr)
        if risk.models:
            levels = [m.level for m in risk.models if m.level != "N/A"]
            risk.level = max(levels, key=lambda l: LEVEL_ORDER[l], default="LOW")
            if risk.level == "LOW" and "MITIGATED" in levels:
                risk.level = "MITIGATED"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(asdict(risk), indent=1))
        except OSError:
            pass
        return risk

    def _resolve_vmt(self, vpk: VPK, candidates: List[str], others: List[AddonInfo]) -> Tuple[Optional[str], str, Optional[bytes]]:
        for c in candidates:
            e = vpk.find(c)
            if e:
                return c, "addon", vpk.read(e)
        for c in candidates:
            for o in others:
                try:
                    with VPK(o.path) as ov:
                        e = ov.find(c)
                        if e:
                            return c, f"other-addon:{o.display_name}", ov.read(e)
                except (VPKError, OSError):
                    continue
        for c in candidates:
            data = self.game.read_base_file(c)
            if data is not None:
                return c, "base", data
        return None, "missing", None

    def _analyze_model(self, vpk: VPK, mdl_path: str, category: str, others: List[AddonInfo]) -> Optional[ModelRisk]:
        try:
            mdl = parse_mdl(vpk.read(mdl_path), mdl_path)
        except (ModelError, VPKError, Exception) as e:
            return ModelRisk(mdl_path, category, 0, 0, 0, 0, 0, 0, None, None, None, None, "N/A", [], [], error=f"unreadable MDL: {e}")
        vtxp, vvdp = companion_paths(mdl_path)
        vtx = vvd = None
        try:
            e = vpk.find(vtxp)
            if e:
                vtx = parse_vtx(vpk.read(e), vtxp, mdl.version)
            else:
                d = self.game.read_base_file(vtxp)
                if d:
                    vtx = parse_vtx(d, vtxp, mdl.version)
        except (ModelError, Exception) as ex:
            vtx = None
        try:
            e = vpk.find(vvdp)
            if e:
                vvd = parse_vvd(vpk.read(e), vvdp)
            else:
                d = self.game.read_base_file(vvdp)
                if d:
                    vvd = parse_vvd(d, vvdp)
        except (ModelError, Exception):
            vvd = None
        st = analyze_model(mdl, vtx, vvd)
        if st.lod0_triangles == 0:
            return None  # animation-only / include model: nothing to decal

        stock = self.stock_stats(mdl_path) if category != "viewmodel" else None
        stock_worst = None
        stock_tris = None
        stock_path = None
        if stock and stock.lod0_triangles:
            stock_path = mdl_path
            stock_tris = stock.lod0_triangles
            stock_worst = max((_cost(m.triangles, m.density, A_EFF_BIG_SPLAT) for m in stock.materials), default=0)
        else:
            b = self.baselines().get(category if category in DECAL_CATEGORIES else "other")
            if b:
                stock_worst = int(b["worst_indices"])
                stock_tris = int(b["lod0_triangles"])

        mats: List[MaterialRisk] = []
        worst = 0
        for ms in st.materials:
            big = _cost(ms.triangles, ms.density, A_EFF_BIG_SPLAT)
            small = _cost(ms.triangles, ms.density, A_EFF_SMALL_IMPACT)
            worst = max(worst, big)
            vmt_path, source, data = self._resolve_vmt(vpk, ms.vmt_candidates, others)
            has_nodecal: Optional[bool] = None
            if data is not None:
                try:
                    f = find_nodecal(decode_text(data))
                    has_nodecal = bool(f and f[2].strip().lower() in ("1", "1.0", "true"))
                except Exception:
                    has_nodecal = None
            mats.append(MaterialRisk(
                slot=ms.slot, texture_names=ms.texture_names, vmt_path=vmt_path, vmt_source=source,
                vmt_candidates=ms.vmt_candidates, triangles=ms.triangles, vertices=ms.vertices,
                area=round(ms.area, 1), density=round(ms.density, 4),
                indices_per_big_splat=big, indices_per_small_impact=small,
                splats_to_overflow=round(INDEX_BUFFER_SIZE / big, 1) if big else float("inf"),
                has_nodecal=has_nodecal,
                recommend_nodecal=(big >= self.selective_threshold and not has_nodecal),
            ))
        ratio = (worst / stock_worst) if stock_worst else None
        level, reasons = classify(worst, ratio, category)
        if level in ("MODERATE", "HIGH", "CRITICAL"):
            dense = [m for m in mats if m.indices_per_big_splat >= self.selective_threshold]
            if dense and all(m.has_nodecal for m in dense):
                level = "MITIGATED"
                reasons.append("every dense material already carries $nodecal 1 (author applied the fix)")
        if category == "weapon" and level in ("MODERATE", "HIGH"):
            # dropped world weapons rarely collect decals; only flag the extreme case
            reasons.append("world weapon models rarely receive decals; downgraded")
            level = "LOW"
        notes = list(st.notes)
        if st.num_lods <= 1 and category in ("survivor", "infected"):
            notes.append("no LODs: full-detail mesh is used at every distance")
        if not st.checksum_ok:
            notes.append("checksum mismatch between .mdl/.vtx/.vvd")
        return ModelRisk(
            path=mdl_path, category=category, lod0_triangles=st.lod0_triangles, lod0_vertices=st.lod0_vertices,
            num_lods=st.num_lods, numbones=st.numbones, bodygroups=st.bodygroup_count,
            worst_indices_per_big_splat=worst, stock_path=stock_path, stock_lod0_triangles=stock_tris,
            stock_worst_indices_per_big_splat=stock_worst, ratio_vs_stock=round(ratio, 2) if ratio else None,
            level=level, reasons=reasons, materials=mats, notes=notes,
        )

    # --------------------------------------------------------------- scan
    def scan(self, addons: List[AddonInfo], include_disabled: bool = False, use_cache: bool = True,
             progress=None) -> ScanReport:
        t0 = time.time()
        results: List[AddonRisk] = []
        todo = [a for a in addons if include_disabled or a.is_enabled]
        for i, a in enumerate(todo):
            if progress:
                progress(i, len(todo), a)
            if a.error:
                results.append(AddonRisk(str(a.path), a.entry, a.title, a.author, a.workshop_id, a.is_workshop,
                                         a.enabled, a.size, "", "N/A", [], error=a.error))
                continue
            results.append(self.analyze_addon(a, addons, use_cache=use_cache))
        # conflicts: same model path from several enabled add-ons
        providers: Dict[str, List[str]] = {}
        for r in results:
            if r.enabled is False:
                continue
            for m in r.models:
                providers.setdefault(m.path.lower(), []).append(r.display_name)
        conflicts = {k: v for k, v in providers.items() if len(v) > 1}
        warnings: List[str] = []
        if not self.game.base_vpks():
            warnings.append("base game pak01_dir.vpk not found: stock comparisons unavailable")
        self.log(f"scanned {len(todo)} add-ons in {time.time() - t0:.1f}s")
        return ScanReport(
            generated=time.strftime("%Y-%m-%d %H:%M:%S"), game_dir=str(self.game.root), addons=results,
            conflicts=conflicts, baselines=self.baselines(),
            parameters={"index_buffer_size": INDEX_BUFFER_SIZE, "a_eff_big_splat": A_EFF_BIG_SPLAT,
                        "a_eff_small_impact": A_EFF_SMALL_IMPACT, "selective_threshold": self.selective_threshold},
            warnings=warnings,
        )


# ------------------------------------------------------------------ report
def format_report(report: ScanReport, verbose: bool = False, min_level: str = "LOW") -> str:
    lines: List[str] = []
    lines.append(f"L4D2 Index Buffer Fixer - scan report ({report.generated})")
    lines.append(f"game: {report.game_dir}")
    for w in report.warnings:
        lines.append(f"WARNING: {w}")
    counts = {l: 0 for l in LEVELS}
    for a in report.addons:
        counts[a.level] = counts.get(a.level, 0) + 1
    lines.append("add-ons: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    bl = report.baselines
    if bl:
        lines.append("Valve reference (median): " + "; ".join(
            f"{k}: ~{int(v['worst_indices'])} idx/splat, {int(v['lod0_triangles'])} tris" for k, v in bl.items() if k in ("survivor", "infected")))
    lines.append("")
    order = sorted(report.addons, key=lambda a: -LEVEL_ORDER.get(a.level, 0) if a.level != "N/A" else 1)
    for a in order:
        if a.level == "N/A" and not a.error and not a.models:
            continue
        if LEVEL_ORDER.get(a.level, 0) < LEVEL_ORDER[min_level] and a.level != "N/A":
            continue
        state = "enabled" if a.enabled is not False else "DISABLED"
        lines.append(f"[{a.level}] {a.display_name}  ({state}, {a.size / 1e6:.0f} MB, {Path(a.path).name})")
        if a.error:
            lines.append(f"    error: {a.error}")
        for m in a.models:
            if m.level == "N/A" and not verbose:
                continue
            if m.stock_path:
                stock = f", Valve original {m.stock_lod0_triangles} tris"
                ratio = f", {m.ratio_vs_stock}x its decal cost" if m.ratio_vs_stock else ""
            else:
                stock = ""
                ratio = f", {m.ratio_vs_stock}x Valve {m.category} median" if (m.ratio_vs_stock and m.category in ("survivor", "infected")) else ""
            lines.append(f"    {m.level:<8} {m.path}: {m.lod0_triangles} tris @LOD0 ({m.num_lods} LOD{'s' if m.num_lods != 1 else ''}{stock}{ratio}); worst material ~{m.worst_indices_per_big_splat} idx/splat")
            if m.error:
                lines.append(f"             {m.error}")
            for r in m.reasons:
                lines.append(f"             - {r}")
            for n in m.notes:
                lines.append(f"             note: {n}")
            if verbose or m.level in ("HIGH", "CRITICAL"):
                for mat in m.materials:
                    if not verbose and not mat.recommend_nodecal and not mat.has_nodecal:
                        continue
                    nd = "has $nodecal" if mat.has_nodecal else ("-> $nodecal" if mat.recommend_nodecal else "keep decals")
                    where = mat.vmt_path or "(VMT not found: " + ", ".join(mat.vmt_candidates[:2]) + ")"
                    lines.append(f"             {nd:<12} {where} [{mat.vmt_source}] {mat.triangles} tris, {mat.density * 100:.1f} tris/100u2, ~{mat.indices_per_big_splat} idx/splat ({mat.splats_to_overflow} to overflow)")
        lines.append("")
    if report.conflicts:
        lines.append("Conflicting replacements (same model from several enabled add-ons):")
        for k, v in sorted(report.conflicts.items()):
            lines.append(f"  {k}: " + " | ".join(v))
    return "\n".join(lines)
