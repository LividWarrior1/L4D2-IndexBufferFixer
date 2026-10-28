"""
Apply and revert the material-level mitigation.

Three delivery modes (never touching Workshop downloads in place):

``replace``  (default)  Write ``addons/ibfix_<name>.vpk``: a complete copy of the
                        add-on with ``"$nodecal" "1"`` added to the selected
                        VMTs, then set the original to "0" and the copy to "1"
                        in addonlist.txt.  Works regardless of add-on load
                        order because the original is no longer loaded.

``overlay``             Write ``addons/ibfix_<name>_overlay.vpk`` containing ONLY
                        the patched VMTs.  Tiny, but relies on files in
                        ``addons/`` taking precedence over ``addons/workshop/``
                        (true on current builds; verify with the ``path``
                        console command).  Not offered for local add-ons.

``inplace``             Local add-ons only (your own VPKs in ``addons/``): the
                        original is copied to the backup folder and the file
                        is rewritten with patched VMTs under the same name.

Every operation is recorded in ``manifest.json`` so ``revert`` can undo it
exactly: output files are deleted, addonlist.txt entries restored, in-place
originals copied back.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .analyzer import AddonRisk, LEVEL_ORDER, ModelRisk, ScanReport
from .gamefiles import GameInstall, quick_hash
from .keyvalues import decode_text, encode_like, patch_vmt_nodecal, set_addonlist_values
from .vpk import VPK, VPKError, copy_vpk_with_replacements, write_vpk_v1

MODES = ("replace", "overlay", "inplace")


class FixError(Exception):
    pass


@dataclass
class VMTChange:
    path: str
    source: str          # addon | base | other-addon:<name>
    action: str          # added | changed | unchanged | skipped
    model: str
    reason: str


@dataclass
class Operation:
    id: str
    timestamp: str
    mode: str
    source_vpk: str
    source_entry: str
    source_hash: str
    source_title: str
    output_vpk: Optional[str]
    output_entry: Optional[str]
    backup_of_original: Optional[str]
    addonlist_before: Dict[str, str]      # entry -> previous value (only entries we changed)
    addonlist_backup: Optional[str]
    vmts: List[VMTChange]
    models: List[str]
    note: str = ""


@dataclass
class Manifest:
    version: int = 1
    operations: List[Operation] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Manifest":
        if not path.exists():
            return Manifest()
        d = json.loads(path.read_text(encoding="utf-8"))
        ops = []
        for o in d.get("operations", []):
            vm = [VMTChange(**v) for v in o.pop("vmts", [])]
            ops.append(Operation(vmts=vm, **o))
        return Manifest(version=d.get("version", 1), operations=ops)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)


@dataclass
class PlannedFix:
    addon: AddonRisk
    mode: str
    models: List[ModelRisk]
    vmt_targets: Dict[str, Tuple[str, str]]   # vmt path -> (source, reason)
    output_name: str
    estimated_bytes: int
    already_fixed_by: Optional[str] = None     # op id if an up-to-date override exists
    stale_op: Optional[str] = None             # op id whose source changed (needs refresh)
    skipped_reason: str = ""


def _safe_stem(addon: AddonRisk) -> str:
    stem = Path(addon.path).stem
    if addon.workshop_id:
        title = re.sub(r"[^A-Za-z0-9]+", "_", addon.title or "").strip("_")[:40]
        stem = f"{addon.workshop_id}_{title}" if title else addon.workshop_id
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)


def _retitle_addoninfo(data: bytes, suffix: str) -> bytes:
    text = decode_text(data)
    m = re.search(r'(addontitle\s*"?)([^"\r\n]*)("?)', text, re.I)
    if not m:
        return data
    title = m.group(2).rstrip()
    if suffix in title:
        return data
    new = text[:m.start(2)] + f"{title} {suffix}" + text[m.end(2):]
    # addontitle in quotes if it was unquoted and now contains spaces
    if not m.group(1).endswith('"'):
        new = text[:m.start(1)] + m.group(1) + f'"{title} {suffix}"' + text[m.end(3):]
    return encode_like(new, data)


class Fixer:
    def __init__(self, game: GameInstall, data_dir: Path, log=print):
        self.game = game
        self.data_dir = Path(data_dir)
        self.backup_dir = self.data_dir / "backups"
        self.manifest_path = self.data_dir / "manifest.json"
        self.manifest = Manifest.load(self.manifest_path)
        self.log = log

    # ------------------------------------------------------------ planning
    def plan(self, report: ScanReport, min_level: str = "HIGH", all_materials: bool = False,
             mode: str = "replace", only: Optional[List[str]] = None,
             selective_threshold: Optional[int] = None, include_disabled: bool = False) -> List[PlannedFix]:
        if mode not in MODES:
            raise FixError(f"unknown mode {mode}")
        thr = selective_threshold if selective_threshold is not None else int(report.parameters.get("selective_threshold", 4096))
        plans: List[PlannedFix] = []
        for a in report.addons:
            if only and not any(o.lower() in (a.path.lower(), a.entry.lower(), (a.workshop_id or "").lower(), a.title.lower()) for o in only):
                continue
            if a.enabled is False and not include_disabled and not only:
                continue
            if a.error or a.level in ("N/A", "MITIGATED"):
                continue
            if LEVEL_ORDER.get(a.level, 0) < LEVEL_ORDER[min_level]:
                continue
            if Path(a.path).name.lower().startswith("ibfix_"):
                continue  # never fix our own outputs
            models = [m for m in a.models if m.level not in ("N/A", "LOW", "MITIGATED")
                      and LEVEL_ORDER.get(m.level, 0) >= LEVEL_ORDER[min_level]]
            if not models:
                continue
            targets: Dict[str, Tuple[str, str]] = {}
            for m in models:
                for mat in m.materials:
                    if not mat.vmt_path:
                        continue
                    if mat.has_nodecal:
                        continue
                    pick = all_materials or mat.indices_per_big_splat >= thr
                    if pick:
                        why = f"{mat.triangles} tris, ~{mat.indices_per_big_splat} indices per splat"
                        targets.setdefault(mat.vmt_path, (mat.vmt_source, f"{m.path}: {why}"))
            eff_mode = mode
            if not a.is_workshop and mode == "overlay":
                eff_mode = "replace"   # local-vs-local precedence is undefined; use replace
            if a.is_workshop and mode == "inplace":
                eff_mode = "replace"   # never rewrite Workshop downloads
            stem = _safe_stem(a)
            out_name = {"replace": f"ibfix_{stem}.vpk", "overlay": f"ibfix_{stem}_overlay.vpk", "inplace": Path(a.path).name}[eff_mode]
            est = a.size if eff_mode in ("replace", "inplace") else sum(2048 for _ in targets) + 4096
            pf = PlannedFix(a, eff_mode, models, targets, out_name, est)
            # existing operations for this source
            for op in self.manifest.operations:
                if Path(op.source_vpk).resolve() == Path(a.path).resolve() or op.source_entry.lower() == a.entry.lower():
                    if op.source_hash == a.hash and op.mode == eff_mode:
                        pf.already_fixed_by = op.id
                    else:
                        pf.stale_op = op.id
            if not targets:
                pf.skipped_reason = "no VMT could be located for the dense materials"
            plans.append(pf)
        return plans

    # -------------------------------------------------------------- apply
    def _backup_addonlist(self) -> Optional[str]:
        if not self.game.addonlist_path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dst = self.backup_dir / f"addonlist.{time.strftime('%Y%m%d-%H%M%S')}.{uuid.uuid4().hex[:6]}.txt"
        shutil.copy2(self.game.addonlist_path, dst)
        return str(dst)

    def _write_addonlist(self, changes: Dict[str, str]) -> Dict[str, str]:
        text = decode_text(self.game.addonlist_path.read_bytes()) if self.game.addonlist_path.exists() else '"AddonList"\n{\n}\n'
        new, prev = set_addonlist_values(text, changes)
        raw = self.game.addonlist_path.read_bytes() if self.game.addonlist_path.exists() else b""
        tmp = self.game.addonlist_path.with_suffix(".txt.tmp")
        tmp.write_bytes(encode_like(new, raw))
        tmp.replace(self.game.addonlist_path)
        return prev

    def _patched_vmts(self, pf: PlannedFix) -> Tuple[Dict[str, bytes], List[VMTChange]]:
        patched: Dict[str, bytes] = {}
        changes: List[VMTChange] = []
        with VPK(pf.addon.path) as src:
            for vmt_path, (source, reason) in pf.vmt_targets.items():
                data: Optional[bytes] = None
                e = src.find(vmt_path)
                if e:
                    data = src.read(e)
                elif source.startswith("other-addon:"):
                    # locate in the named add-on
                    for a in self.game.list_addons(with_info=False):
                        try:
                            with VPK(a.path) as ov:
                                oe = ov.find(vmt_path)
                                if oe:
                                    data = ov.read(oe)
                                    break
                        except (VPKError, OSError):
                            continue
                if data is None:
                    data = self.game.read_base_file(vmt_path)
                if data is None:
                    changes.append(VMTChange(vmt_path, source, "skipped", reason, "VMT not found anywhere"))
                    continue
                text = decode_text(data)
                new_text, action = patch_vmt_nodecal(text)
                changes.append(VMTChange(vmt_path, source, action, reason.split(":")[0], reason))
                if action in ("added", "changed"):
                    patched[vmt_path] = encode_like(new_text, data)
        return patched, changes

    def apply(self, plans: List[PlannedFix], dry_run: bool = True, refresh: bool = False) -> List[Operation]:
        done: List[Operation] = []
        addonlist_backup: Optional[str] = None
        for pf in plans:
            a = pf.addon
            if pf.skipped_reason:
                self.log(f"skip {a.display_name}: {pf.skipped_reason}")
                continue
            if pf.already_fixed_by and not refresh:
                self.log(f"skip {a.display_name}: already fixed by operation {pf.already_fixed_by} (use --refresh to redo)")
                continue
            if pf.stale_op:
                self.log(f"{a.display_name}: previous fix {pf.stale_op} is stale (add-on changed) - reverting it first")
                if not dry_run:
                    self.revert([pf.stale_op])
            out_path = self.game.addons_dir / pf.output_name
            self.log(f"{'[dry-run] ' if dry_run else ''}{pf.mode}: {a.display_name}")
            self.log(f"    -> {out_path}  (~{pf.estimated_bytes / 1e6:.0f} MB)")
            for p, (src, why) in pf.vmt_targets.items():
                self.log(f"    $nodecal 1  {p}  [{src}]  ({why})")
            if dry_run:
                continue

            patched, changes = self._patched_vmts(pf)
            if not patched:
                self.log(f"    nothing to patch for {a.display_name} (all materials already carry $nodecal?)")
                continue
            self.game.addons_dir.mkdir(parents=True, exist_ok=True)
            op = Operation(
                id=uuid.uuid4().hex[:10], timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), mode=pf.mode,
                source_vpk=str(a.path), source_entry=a.entry, source_hash=a.hash, source_title=a.title,
                output_vpk=None, output_entry=None, backup_of_original=None, addonlist_before={},
                addonlist_backup=None, vmts=changes, models=[m.path for m in pf.models],
            )
            try:
                with VPK(a.path) as src:
                    additions: Dict[str, bytes] = {}
                    info_entry = src.find("addoninfo.txt")
                    if pf.mode == "replace":
                        if info_entry:
                            patched["addoninfo.txt"] = _retitle_addoninfo(src.read(info_entry), "[IBFix]")
                        copy_vpk_with_replacements(src, out_path, patched)
                        op.output_vpk = str(out_path)
                        op.output_entry = out_path.name
                    elif pf.mode == "overlay":
                        files = dict(patched)
                        base_info = src.read(info_entry) if info_entry else b'"AddonInfo"\n{\n\taddonSteamAppID 550\n\taddontitle "override"\n}\n'
                        files["addoninfo.txt"] = _retitle_addoninfo(base_info, "[IBFix overlay]")
                        write_vpk_v1(out_path, files)
                        op.output_vpk = str(out_path)
                        op.output_entry = out_path.name
                    elif pf.mode == "inplace":
                        self.backup_dir.mkdir(parents=True, exist_ok=True)
                        bak = self.backup_dir / f"{Path(a.path).stem}.{time.strftime('%Y%m%d-%H%M%S')}.orig.vpk"
                        shutil.copy2(a.path, bak)
                        op.backup_of_original = str(bak)
                        tmp_out = out_path.with_suffix(".vpk.ibfixtmp")
                        copy_vpk_with_replacements(src, tmp_out, patched)
                        src.close()
                        tmp_out.replace(out_path)
                        op.output_vpk = str(out_path)
                        op.output_entry = None
                # addonlist changes
                changes_al: Dict[str, str] = {}
                if pf.mode == "replace":
                    changes_al[a.entry] = "0"
                    changes_al[out_path.name] = "1"
                elif pf.mode == "overlay":
                    changes_al[out_path.name] = "1"
                if changes_al:
                    if addonlist_backup is None:
                        addonlist_backup = self._backup_addonlist()
                    op.addonlist_backup = addonlist_backup
                    op.addonlist_before = self._write_addonlist(changes_al)
                    # entries we appended (not previously present) are recorded as "" so revert removes them
                    for k in changes_al:
                        if k not in op.addonlist_before and not any(k.lower() == pk.lower() for pk in op.addonlist_before):
                            op.addonlist_before[k] = ""
            except Exception as e:  # keep the manifest consistent even on failure
                self.log(f"    FAILED: {e}")
                if op.output_vpk and Path(op.output_vpk).exists() and pf.mode != "inplace":
                    try:
                        Path(op.output_vpk).unlink()
                    except OSError:
                        pass
                raise
            self.manifest.operations.append(op)
            self.manifest.save(self.manifest_path)
            done.append(op)
            self.log(f"    done: {len(patched)} file(s) patched -> {out_path.name}")
        return done

    # -------------------------------------------------------------- revert
    def revert(self, op_ids: Optional[List[str]] = None, dry_run: bool = False) -> List[Operation]:
        reverted: List[Operation] = []
        for op in list(self.manifest.operations):
            if op_ids and op.id not in op_ids:
                continue
            self.log(f"{'[dry-run] ' if dry_run else ''}revert {op.id} ({op.mode}: {op.source_title or op.source_entry})")
            if dry_run:
                continue
            # 1. output file
            if op.output_vpk and op.mode in ("replace", "overlay"):
                p = Path(op.output_vpk)
                if p.exists():
                    p.unlink()
                    self.log(f"    deleted {p.name}")
            if op.mode == "inplace" and op.backup_of_original:
                bak = Path(op.backup_of_original)
                if bak.exists():
                    shutil.copy2(bak, op.output_vpk or op.source_vpk)
                    self.log(f"    restored original {Path(op.source_vpk).name} from backup")
                else:
                    self.log(f"    WARNING: backup {bak} missing; {op.source_vpk} left as is")
            # 2. addonlist entries
            if op.addonlist_before and self.game.addonlist_path.exists():
                text = decode_text(self.game.addonlist_path.read_bytes())
                restore = {k: v for k, v in op.addonlist_before.items() if v in ("0", "1")}
                remove = [k for k, v in op.addonlist_before.items() if v == ""]
                new, _ = set_addonlist_values(text, restore)
                if remove:
                    lines = new.splitlines(keepends=True)
                    keep = []
                    for line in lines:
                        if any(f'"{k}"'.lower() in line.lower() for k in remove):
                            continue
                        keep.append(line)
                    new = "".join(keep)
                raw = self.game.addonlist_path.read_bytes()
                self.game.addonlist_path.write_bytes(encode_like(new, raw))
                self.log(f"    addonlist.txt restored ({len(restore)} entr{'y' if len(restore) == 1 else 'ies'} reset, {len(remove)} removed)")
            self.manifest.operations.remove(op)
            self.manifest.save(self.manifest_path)
            reverted.append(op)
        return reverted

    def status(self) -> List[Tuple[Operation, str]]:
        """Each recorded operation with 'ok' | 'stale' | 'output-missing'."""
        out = []
        for op in self.manifest.operations:
            state = "ok"
            src = Path(op.source_vpk)
            if op.output_vpk and not Path(op.output_vpk).exists():
                state = "output-missing"
            elif src.exists() and quick_hash(src) != op.source_hash and op.mode != "inplace":
                state = "stale"
            elif not src.exists() and op.mode != "inplace":
                state = "source-missing"
            out.append((op, state))
        return out
