"""
Crash diagnostics helpers.

* Parse ``left4dead2/console.log`` (written when the game runs with the
  ``-condebug`` launch option) for index-buffer errors and the surrounding
  context (map, models mentioned, time).
* List crash dumps (``*.mdmp``) so the user can correlate timestamps.
* Generate a probe .cfg that asks the engine to print the help text of the
  decal cvars; parse the resulting console output so flags/defaults are
  confirmed from *this* game build instead of assumed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .gamefiles import GameInstall
from .keyvalues import decode_text

ERROR_RE = re.compile(r"too many (indices|vertices) for (index|vertex) buffer.*?\((\d+)\s*>\s*(\d+)\)", re.I)
MAP_RE = re.compile(r"^(?:Host_NewGame on|Map:|Loading map|---- Host_NewGame ----|\*\*\* Map Load:)\s*(\S+)", re.I | re.M)
MODEL_RE = re.compile(r"models/[A-Za-z0-9_./\\-]+\.mdl", re.I)
HELP_RE = re.compile(r'^"(?P<name>[A-Za-z0-9_]+)"\s*=\s*"(?P<value>[^"]*)"(?:\s*\(\s*def\.\s*"(?P<default>[^"]*)"\s*\))?(?P<flags>[^\n]*)$', re.M)

# cvars confirmed present in this engine build's strings (engine.dll); flags/defaults
# are only known once the probe has been run in-game.
PROBE_CVARS = [
    "r_drawmodeldecals", "r_maxmodeldecal", "r_decals", "r_drawdecals", "r_decal_overlap_count",
    "r_decal_overlap_area", "r_decal_cover_count", "cl_player_max_decal_count", "r_cleardecals",
]
PROBE_END = "IBFIX_PROBE_END"


@dataclass
class IndexBufferEvent:
    line_no: int
    kind: str              # 'index' | 'vertex'
    requested: int
    limit: int
    context_before: List[str]
    last_map: Optional[str]
    models_nearby: List[str]


@dataclass
class ProbeResult:
    cvars: Dict[str, Dict[str, str]] = field(default_factory=dict)   # name -> {value, default, flags}
    found_end: bool = False


@dataclass
class Diagnosis:
    console_log: Optional[str]
    console_log_age_hours: Optional[float]
    events: List[IndexBufferEvent]
    crash_dumps: List[str]
    probe: Optional[ProbeResult]
    hints: List[str]


def parse_console_log(text: str, context_lines: int = 12) -> List[IndexBufferEvent]:
    lines = text.splitlines()
    events: List[IndexBufferEvent] = []
    last_map: Optional[str] = None
    for i, line in enumerate(lines):
        m = MAP_RE.search(line)
        if m:
            last_map = m.group(1)
        e = ERROR_RE.search(line)
        if e:
            before = lines[max(0, i - context_lines):i]
            models = []
            for l in lines[max(0, i - 200):i + 1]:
                for mm in MODEL_RE.findall(l):
                    mm = mm.replace("\\", "/").lower()
                    if mm not in models:
                        models.append(mm)
            events.append(IndexBufferEvent(
                line_no=i + 1, kind="index" if e.group(2).lower() == "index" else "vertex",
                requested=int(e.group(3)), limit=int(e.group(4)), context_before=before,
                last_map=last_map, models_nearby=models[-12:],
            ))
    return events


_HELP_LINE = re.compile(r'^"(?P<name>[A-Za-z0-9_]+)"(?:\s*=\s*"(?P<value>[^"]*)")?(?:\s*\(\s*def\.\s*"(?P<default>[^"]*)"\s*\))?(?P<rest>.*)$')


def parse_probe_output(text: str) -> ProbeResult:
    """
    L4D2 prints `help` results over several lines:
        "r_drawdecals" = "1"
         cheat
         - Render decals.
    i.e. flags and the description follow on indented lines.  Only the last
    probe block in the log is used.
    """
    pr = ProbeResult()
    start = text.rfind("IBFIX_PROBE_START")
    block = text[start:] if start >= 0 else text
    end = block.find(PROBE_END)
    if end >= 0:
        block = block[:end]
        pr.found_end = True
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        m = _HELP_LINE.match(lines[i].strip()) if lines[i].startswith('"') else None
        if m and m.group("name") in PROBE_CVARS:
            flags: List[str] = []
            desc = ""
            rest = m.group("rest").strip()
            if rest and not rest.startswith("-"):
                flags.append(rest)
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or lines[j].startswith("\t")):
                t = lines[j].strip()
                if t.startswith("- "):
                    desc = t[2:].strip()
                elif t:
                    flags.append(t)
                j += 1
            pr.cvars[m.group("name")] = {
                "value": m.group("value") if m.group("value") is not None else "",
                "default": m.group("default") or "",
                "flags": " ".join(flags),
                "description": desc,
            }
            i = j
            continue
        i += 1
    return pr


def write_probe_cfg(game: GameInstall) -> Path:
    game.cfg_dir.mkdir(parents=True, exist_ok=True)
    p = game.cfg_dir / "ibfix_probe.cfg"
    body = ["// Written by L4D2 Index Buffer Fixer. Run from the console:  exec ibfix_probe",
            "// (launch the game with -condebug so the output lands in console.log)",
            'echo "IBFIX_PROBE_START"']
    body += [f"help {c}" for c in PROBE_CVARS]
    body.append(f'echo "{PROBE_END}"')
    p.write_text("\n".join(body) + "\n", encoding="ascii")
    return p


def diagnose(game: GameInstall) -> Diagnosis:
    hints: List[str] = []
    events: List[IndexBufferEvent] = []
    probe: Optional[ProbeResult] = None
    log_path = None
    age = None
    if game.console_log.exists():
        log_path = str(game.console_log)
        age = (time.time() - game.console_log.stat().st_mtime) / 3600.0
        try:
            text = decode_text(game.console_log.read_bytes())
        except OSError:
            text = ""
        events = parse_console_log(text)
        probe = parse_probe_output(text)
        if not probe.cvars:
            probe = None
            hints.append("No probe output in console.log yet: run `exec ibfix_probe` in the console (after `ibfix probe`).")
    else:
        hints.append("No left4dead2/console.log: add -condebug to the game's launch options so crashes and probe output are logged.")
    dumps = [str(p) for p in game.crash_dumps()]
    if events:
        worst = max(e.requested for e in events)
        hints.append(f"{len(events)} index/vertex buffer error(s) in console.log; largest request {worst} indices.")
        if any(e.requested > 65536 for e in events):
            hints.append("Requests above 65536 could not fit even a doubled buffer: only removing decals from the dense model helps.")
    if probe and probe.cvars:
        rd = probe.cvars.get("r_drawmodeldecals")
        if rd:
            if "cheat" in rd["flags"].lower():
                hints.append("r_drawmodeldecals is cheat-protected on this build: cannot be used on normal servers.")
            else:
                hints.append("r_drawmodeldecals is NOT cheat-protected: `r_drawmodeldecals 0` disables all model decals (global equivalent of $nodecal).")
        rm = probe.cvars.get("r_maxmodeldecal")
        if rm and "cheat" not in rm["flags"].lower():
            hints.append(f"r_maxmodeldecal (currently {rm['value']}) is adjustable: lowering it caps how many decals one model can accumulate.")
        for name in ("r_drawmodeldecals", "r_maxmodeldecal", "cl_player_max_decal_count"):
            v = probe.cvars.get(name)
            if v and "archive" not in v["flags"].lower() and "cheat" not in v["flags"].lower():
                hints.append(f"{name} is not archived: put it in cfg/autoexec.cfg to make a change persistent.")
                break
    return Diagnosis(log_path, age, events, dumps, probe, hints)


def format_diagnosis(d: Diagnosis) -> str:
    out: List[str] = ["L4D2 Index Buffer Fixer - diagnostics"]
    if d.console_log:
        out.append(f"console.log: {d.console_log} (last written {d.console_log_age_hours:.1f} h ago)")
    else:
        out.append("console.log: not found")
    out.append(f"crash dumps: {len(d.crash_dumps)}")
    for p in d.crash_dumps[-5:]:
        out.append(f"  {p}")
    if d.events:
        out.append(f"index buffer errors found: {len(d.events)}")
        for e in d.events[-10:]:
            out.append(f"  line {e.line_no}: {e.kind} buffer {e.requested} > {e.limit}" + (f"  (map {e.last_map})" if e.last_map else ""))
            if e.models_nearby:
                out.append("     models mentioned shortly before: " + ", ".join(e.models_nearby[-6:]))
    else:
        out.append("index buffer errors found: none in log")
    if d.probe and d.probe.cvars:
        out.append("cvar probe results (from this game build):")
        for k, v in d.probe.cvars.items():
            flags = v["flags"] or "no cheat/archive flags"
            default = f" (default {v['default']})" if v["default"] else ""
            desc = f"  - {v['description']}" if v.get("description") else ""
            out.append(f"  {k} = {v['value'] or '(command)'}{default}  [{flags}]{desc}")
    for h in d.hints:
        out.append(f"hint: {h}")
    return "\n".join(out)
