"""
Add-on bisection: find which add-on triggers a crash by halving the enabled
set, using addonlist.txt as the switch.  State lives in the data folder so
the process survives restarts; ``finish`` always restores the original list.

Because the crash is probabilistic (it needs decals to pile up), each step
should be tested long enough to be meaningful; the tool only does the
bookkeeping.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .gamefiles import GameInstall
from .keyvalues import decode_text, encode_like, set_addonlist_values


@dataclass
class BisectState:
    original_values: Dict[str, str]        # entry -> '0'|'1' as they were when we started
    candidates: List[str]                  # entries still suspected
    currently_enabled: List[str]           # half under test
    history: List[Dict[str, object]] = field(default_factory=list)
    backup_path: str = ""
    started: str = ""

    def save(self, p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @staticmethod
    def load(p: Path) -> Optional["BisectState"]:
        if not p.exists():
            return None
        return BisectState(**json.loads(p.read_text(encoding="utf-8")))


class Bisector:
    def __init__(self, game: GameInstall, data_dir: Path, log=print):
        self.game = game
        self.state_path = Path(data_dir) / "bisect_state.json"
        self.backup_dir = Path(data_dir) / "backups"
        self.log = log

    def _apply(self, enabled: List[str], universe: List[str]) -> None:
        changes = {e: ("1" if e in enabled else "0") for e in universe}
        raw = self.game.addonlist_path.read_bytes()
        new, _ = set_addonlist_values(decode_text(raw), changes)
        self.game.addonlist_path.write_bytes(encode_like(new, raw))

    def start(self, candidates: Optional[List[str]] = None) -> BisectState:
        if self.state_path.exists():
            raise RuntimeError("a bisection is already in progress; run `bisect finish` or `bisect status`")
        entries = self.game.read_addonlist()
        original = {k: v for k, v in entries}
        enabled = [k for k, v in entries if v == "1"]
        cands = [c for c in (candidates or enabled) if c in original]
        if len(cands) < 2:
            raise RuntimeError("need at least two enabled add-ons to bisect")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        bak = self.backup_dir / f"addonlist.bisect.{time.strftime('%Y%m%d-%H%M%S')}.txt"
        shutil.copy2(self.game.addonlist_path, bak)
        st = BisectState(original_values=original, candidates=cands, currently_enabled=cands[: len(cands) // 2],
                         backup_path=str(bak), started=time.strftime("%Y-%m-%d %H:%M:%S"))
        self._apply(st.currently_enabled, cands)
        st.save(self.state_path)
        return st

    def step(self, crashed: bool) -> BisectState:
        st = BisectState.load(self.state_path)
        if not st:
            raise RuntimeError("no bisection in progress")
        st.history.append({"enabled": list(st.currently_enabled), "crashed": crashed})
        if crashed:
            st.candidates = list(st.currently_enabled)
        else:
            st.candidates = [c for c in st.candidates if c not in st.currently_enabled]
        universe = [k for k in st.original_values if st.original_values[k] == "1"]
        if len(st.candidates) <= 1:
            st.currently_enabled = list(st.candidates)
            self._apply(st.currently_enabled, universe)
            st.save(self.state_path)
            return st
        st.currently_enabled = st.candidates[: len(st.candidates) // 2]
        self._apply(st.currently_enabled, universe)
        st.save(self.state_path)
        return st

    def finish(self) -> Optional[BisectState]:
        st = BisectState.load(self.state_path)
        if not st:
            return None
        raw = self.game.addonlist_path.read_bytes()
        new, _ = set_addonlist_values(decode_text(raw), st.original_values)
        self.game.addonlist_path.write_bytes(encode_like(new, raw))
        self.state_path.unlink()
        return st

    def status(self) -> Optional[BisectState]:
        return BisectState.load(self.state_path)
