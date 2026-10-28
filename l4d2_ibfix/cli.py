"""
Command-line interface.

    ibfix scan      [--all] [--verbose] [--include-props] [--no-cache]
    ibfix fix       [--dry-run|--apply] [--mode replace|overlay|inplace] [--level HIGH]
                    [--all-materials] [--threshold N] [--only ID|name ...] [--refresh]
    ibfix status
    ibfix revert    [--all | OPID ...]
    ibfix diagnose
    ibfix probe
    ibfix bisect    start|good|bad|status|finish
    ibfix inspect   PATH.vpk            (analyse one VPK anywhere on disk)

Global options: --game-dir PATH, --data-dir PATH, --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from . import __version__
from .analyzer import Analyzer, DEFAULT_CATEGORIES, DECAL_CATEGORIES, LEVEL_ORDER, ScanReport, format_report
from .bisect import Bisector
from .diagnostics import diagnose, format_diagnosis, write_probe_cfg
from .fixer import Fixer, MODES
from .gamefiles import GameInstall, default_data_dir, find_game_dir


def _game(args) -> GameInstall:
    root = find_game_dir(args.game_dir)
    if root is None:
        sys.exit("Could not find the Left 4 Dead 2 folder. Pass --game-dir \"...\\steamapps\\common\\Left 4 Dead 2\".")
    return GameInstall(root)


def _data_dir(args) -> Path:
    d = Path(args.data_dir) if args.data_dir else default_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _latest_report(data_dir: Path) -> Optional[ScanReport]:
    reports = sorted((data_dir / "reports").glob("scan_*.json"))
    if not reports:
        return None
    return ScanReport.from_json(reports[-1].read_text(encoding="utf-8"))


def _save_report(data_dir: Path, report: ScanReport, text: str) -> Path:
    rd = data_dir / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (rd / f"scan_{stamp}.json").write_text(report.to_json(), encoding="utf-8")
    p = rd / f"scan_{stamp}.txt"
    p.write_text(text, encoding="utf-8")
    return p


def cmd_scan(args) -> int:
    game = _game(args)
    data_dir = _data_dir(args)
    cats = list(DEFAULT_CATEGORIES)
    if args.include_props:
        cats = list(DECAL_CATEGORIES)
    an = Analyzer(game, data_dir, categories=cats, selective_threshold=args.threshold, log=_log(args))
    addons = game.list_addons()
    _log(args)(f"found {len(addons)} add-ons ({sum(1 for a in addons if a.is_enabled)} enabled) in {game.addons_dir}")

    def progress(i, n, a):
        if not args.json:
            sys.stderr.write(f"\r  scanning {i + 1}/{n}: {a.display_name[:60]:<60}")
            sys.stderr.flush()

    report = an.scan(addons, include_disabled=args.all, use_cache=not args.no_cache, progress=progress)
    if not args.json:
        sys.stderr.write("\r" + " " * 90 + "\r")
    text = format_report(report, verbose=args.verbose, min_level=args.min_level)
    saved = _save_report(data_dir, report, text)
    if args.json:
        print(report.to_json())
    else:
        print(text)
        print(f"\nreport saved to {saved}")
        flagged = [a for a in report.addons if a.level in ("HIGH", "CRITICAL")]
        if flagged:
            print(f"\n{len(flagged)} add-on(s) at HIGH/CRITICAL risk. Preview the fix with:  ibfix fix --dry-run")
    game.close()
    return 0


def cmd_fix(args) -> int:
    game = _game(args)
    data_dir = _data_dir(args)
    report = _latest_report(data_dir)
    if report is None or args.rescan:
        _log(args)("no saved scan (or --rescan given): scanning first")
        an = Analyzer(game, data_dir, selective_threshold=args.threshold, log=_log(args))
        addons = game.list_addons()
        report = an.scan(addons, include_disabled=False, use_cache=True)
        _save_report(data_dir, report, format_report(report))
    fixer = Fixer(game, data_dir, log=print)
    plans = fixer.plan(report, min_level=args.level, all_materials=args.all_materials, mode=args.mode,
                       only=args.only, selective_threshold=args.threshold, include_disabled=args.include_disabled)
    if not plans:
        print(f"nothing to fix at level {args.level} or above (run `ibfix scan` to see details)")
        return 0
    dry = not args.apply
    if dry:
        print("DRY RUN - nothing will be written. Re-run with --apply to make these changes.\n")
    ops = fixer.apply(plans, dry_run=dry, refresh=args.refresh)
    if not dry:
        print(f"\n{len(ops)} operation(s) recorded in {fixer.manifest_path}")
        modes = {op.mode for op in ops}
        if "replace" in modes:
            print("Start the game and check Extras > Add-ons: the originals now show as disabled and the patched copies as '[IBFix]'.")
        if "overlay" in modes:
            print("Overlay mode: in the console run `path` and confirm the ibfix_*_overlay.vpk line appears ABOVE the add-on it overrides.")
        if "inplace" in modes:
            print("In-place mode: the original VPK(s) were copied to the backup folder before being rewritten.")
        print("Undo everything with:  ibfix revert --all")
    total = sum(p.estimated_bytes for p in plans if not p.skipped_reason and not p.already_fixed_by)
    if dry and total:
        print(f"\nestimated disk use for new files: ~{total / 1e6:.0f} MB")
    game.close()
    return 0


def cmd_status(args) -> int:
    game = _game(args)
    fixer = Fixer(game, _data_dir(args), log=print)
    rows = fixer.status()
    if not rows:
        print("no fixes applied")
        return 0
    for op, state in rows:
        print(f"{op.id}  {op.timestamp}  {op.mode:<8} {state:<15} {op.source_title or op.source_entry}")
        if op.output_vpk:
            print(f"           -> {op.output_vpk}")
        print(f"           {len([v for v in op.vmts if v.action in ('added', 'changed')])} VMT(s) patched; models: {', '.join(op.models)}")
    stale = [op for op, s in rows if s == "stale"]
    if stale:
        print(f"\n{len(stale)} fix(es) are stale (the add-on was updated): run `ibfix fix --apply --refresh`")
    return 0


def cmd_revert(args) -> int:
    game = _game(args)
    fixer = Fixer(game, _data_dir(args), log=print)
    if not args.all and not args.ops:
        sys.exit("specify operation ids or --all")
    ops = fixer.revert(None if args.all else args.ops, dry_run=args.dry_run)
    print(f"{len(ops)} operation(s) reverted")
    return 0


def cmd_diagnose(args) -> int:
    game = _game(args)
    d = diagnose(game)
    if args.json:
        print(json.dumps(asdict(d), indent=2, default=str))
    else:
        print(format_diagnosis(d))
    return 0


def cmd_probe(args) -> int:
    game = _game(args)
    p = write_probe_cfg(game)
    print(f"wrote {p}")
    print("1. Add -condebug to the game's launch options (Steam > L4D2 > Properties > Launch Options).")
    print("2. Start the game, open the console and run:  exec ibfix_probe")
    print("3. Quit, then run:  ibfix diagnose   - it reads the cvar flags/defaults from console.log.")
    return 0


def cmd_bisect(args) -> int:
    game = _game(args)
    b = Bisector(game, _data_dir(args), log=print)
    if args.action == "start":
        st = b.start()
        print(f"bisecting {len(st.candidates)} enabled add-ons; {len(st.currently_enabled)} enabled for this round.")
        print("Play until you are confident the crash does / does not happen, then run `ibfix bisect bad` or `ibfix bisect good`.")
    elif args.action in ("good", "bad"):
        st = b.step(crashed=(args.action == "bad"))
        if len(st.candidates) <= 1:
            print("suspect: " + (st.candidates[0] if st.candidates else "none of the tested add-ons"))
            print("run `ibfix bisect finish` to restore your original add-on list.")
        else:
            print(f"{len(st.candidates)} candidates left; {len(st.currently_enabled)} enabled for the next round.")
    elif args.action == "status":
        st = b.status()
        if not st:
            print("no bisection in progress")
        else:
            print(f"started {st.started}; {len(st.candidates)} candidates; enabled now: {len(st.currently_enabled)}")
            for h in st.history:
                print(f"  round: {len(h['enabled'])} enabled -> {'CRASHED' if h['crashed'] else 'ok'}")
    elif args.action == "finish":
        st = b.finish()
        print("original addonlist.txt restored" if st else "no bisection in progress")
    return 0


def cmd_inspect(args) -> int:
    """Analyse an arbitrary VPK (no game install needed unless --game-dir is given for stock comparison)."""
    from .gamefiles import AddonInfo
    data_dir = _data_dir(args)
    root = find_game_dir(args.game_dir)
    game = GameInstall(root) if root else GameInstall(Path(args.vpk).parent.parent.parent)
    an = Analyzer(game, data_dir, categories=list(DECAL_CATEGORIES), selective_threshold=args.threshold, log=_log(args))
    p = Path(args.vpk)
    info = AddonInfo(path=p, entry=p.name, is_workshop=False, workshop_id=None, enabled=True, size=p.stat().st_size, mtime=p.stat().st_mtime)
    from .vpk import VPK
    from .gamefiles import read_addoninfo
    with VPK(p) as v:
        info.title, info.author, info.content_flags = read_addoninfo(v)
    risk = an.analyze_addon(info, [info], use_cache=not args.no_cache)
    rep = ScanReport(time.strftime("%Y-%m-%d %H:%M:%S"), str(game.root), [risk], {}, an.baselines() if root else {}, {"selective_threshold": args.threshold})
    print(rep.to_json() if args.json else format_report(rep, verbose=True))
    return 0


def _log(args):
    if args.json:
        return lambda *a, **k: None
    return lambda *a, **k: print(*a, **k, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ibfix", description="L4D2 Index Buffer Fixer - find and defuse add-on models that overflow the engine's dynamic index buffer when decals hit them.")
    ap.add_argument("--version", action="version", version=f"ibfix {__version__}")
    ap.add_argument("--game-dir", help="Left 4 Dead 2 install folder (auto-detected from Steam if omitted)")
    ap.add_argument("--data-dir", help="where reports, backups and the manifest live (default: %%LOCALAPPDATA%%\\L4D2IndexBufferFixer)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="analyse installed add-ons")
    s.add_argument("--all", action="store_true", help="include add-ons disabled in addonlist.txt")
    s.add_argument("--verbose", "-v", action="store_true", help="list every material of every model")
    s.add_argument("--include-props", action="store_true", help="also analyse prop models (slow on big map packs)")
    s.add_argument("--no-cache", action="store_true", help="ignore cached per-add-on results")
    s.add_argument("--threshold", type=int, default=4096, help="indices-per-splat above which a material gets $nodecal in selective mode (default 4096 = 1/8 of the buffer)")
    s.add_argument("--min-level", default="MITIGATED", choices=list(LEVEL_ORDER), help="hide add-ons below this level in the text report")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("fix", help="create patched overrides for risky add-ons (dry-run unless --apply)")
    f.add_argument("--apply", action="store_true", help="actually write files (default is a dry run)")
    f.add_argument("--dry-run", action="store_true", help="(default) show what would change")
    f.add_argument("--mode", default="replace", choices=MODES, help="replace (default, safest), overlay (tiny, relies on load order), inplace (local add-ons only)")
    f.add_argument("--level", default="HIGH", choices=["MODERATE", "HIGH", "CRITICAL"], help="fix add-ons at this risk level or above (default HIGH)")
    f.add_argument("--all-materials", action="store_true", help="put $nodecal on every material of a flagged model, not just the dense ones")
    f.add_argument("--threshold", type=int, default=None, help="override selective threshold (indices per splat)")
    f.add_argument("--only", nargs="*", help="restrict to these add-ons (workshop id, file name or title)")
    f.add_argument("--refresh", action="store_true", help="redo fixes whose add-on changed since (Workshop update)")
    f.add_argument("--rescan", action="store_true", help="rescan instead of using the last saved report")
    f.add_argument("--include-disabled", action="store_true", help="also fix add-ons currently disabled in addonlist.txt")
    f.set_defaults(func=cmd_fix)

    st = sub.add_parser("status", help="list applied fixes and whether they are still current")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("revert", help="undo fixes")
    r.add_argument("ops", nargs="*", help="operation ids (see status)")
    r.add_argument("--all", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_revert)

    d = sub.add_parser("diagnose", help="read console.log / crash dumps for index buffer errors")
    d.set_defaults(func=cmd_diagnose)

    pr = sub.add_parser("probe", help="write cfg/ibfix_probe.cfg that prints decal cvar flags in-game")
    pr.set_defaults(func=cmd_probe)

    b = sub.add_parser("bisect", help="halve the enabled add-on set to find a culprit")
    b.add_argument("action", choices=["start", "good", "bad", "status", "finish"])
    b.set_defaults(func=cmd_bisect)

    i = sub.add_parser("inspect", help="analyse a single .vpk file")
    i.add_argument("vpk")
    i.add_argument("--threshold", type=int, default=4096)
    i.add_argument("--no-cache", action="store_true")
    i.set_defaults(func=cmd_inspect)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
