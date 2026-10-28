"""
Locate the Left 4 Dead 2 installation and enumerate its add-ons.

Nothing in this module writes to disk.
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .keyvalues import decode_text, kv_get, parse_addonlist, parse_keyvalues
from .vpk import VPK, VPKError

APP_ID = 550
GAME_FOLDER_NAME = "Left 4 Dead 2"
# gameinfo.txt search order (first match wins) - TLS update content lives in dlc3
BASE_GAME_DIRS = ("left4dead2_dlc3", "left4dead2_dlc2", "left4dead2_dlc1", "left4dead2")


# --------------------------------------------------------------- find the game
def _steam_root_from_registry() -> Optional[Path]:
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            val, _ = winreg.QueryValueEx(k, name)
                            p = Path(str(val))
                            if p.exists():
                                return p
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        return None
    return None


def _library_folders(steam_root: Path) -> List[Path]:
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            kv = parse_keyvalues(decode_text(vdf.read_bytes()))
            lf = kv_get(kv, "libraryfolders") or {}
            if isinstance(lf, dict):
                for _k, v in lf.items():
                    path = None
                    if isinstance(v, dict):
                        path = kv_get(v, "path")
                    elif isinstance(v, str):
                        path = v
                    if path:
                        p = Path(path)
                        if p.exists() and p not in libs:
                            libs.append(p)
        except Exception:
            pass
    return libs


def find_game_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """Return the folder containing left4dead2.exe / left4dead2/ (or None)."""
    if explicit:
        p = Path(explicit).expanduser()
        if (p / "left4dead2").is_dir():
            return p
        if p.name.lower() == "left4dead2" and (p / "pak01_dir.vpk").exists():
            return p.parent
        return None
    candidates: List[Path] = []
    root = _steam_root_from_registry()
    if root:
        for lib in _library_folders(root):
            manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
            installdir = GAME_FOLDER_NAME
            if manifest.exists():
                try:
                    kv = parse_keyvalues(decode_text(manifest.read_bytes()))
                    state = kv_get(kv, "AppState") or {}
                    if isinstance(state, dict):
                        installdir = kv_get(state, "installdir", GAME_FOLDER_NAME) or GAME_FOLDER_NAME
                except Exception:
                    pass
            candidates.append(lib / "steamapps" / "common" / installdir)
    env_root = os.environ.get("L4D2_DIR")
    if env_root:
        candidates.insert(0, Path(env_root))
    for drive in ("C:", "D:", "E:", "F:"):
        candidates.append(Path(f"{drive}/Program Files (x86)/Steam/steamapps/common/{GAME_FOLDER_NAME}"))
        candidates.append(Path(f"{drive}/SteamLibrary/steamapps/common/{GAME_FOLDER_NAME}"))
        candidates.append(Path(f"{drive}/Steam/steamapps/common/{GAME_FOLDER_NAME}"))
    home = Path.home()
    candidates.append(home / ".steam" / "steam" / "steamapps" / "common" / GAME_FOLDER_NAME)
    candidates.append(home / ".local" / "share" / "Steam" / "steamapps" / "common" / GAME_FOLDER_NAME)
    candidates.append(home / "mnt" / GAME_FOLDER_NAME)
    for c in candidates:
        if (c / "left4dead2").is_dir():
            return c
    return None


# ------------------------------------------------------------------- add-ons
@dataclass
class AddonInfo:
    path: Path
    entry: str                 # addonlist.txt key: 'workshop\\123.vpk' or 'name.vpk'
    is_workshop: bool
    workshop_id: Optional[str]
    enabled: Optional[bool]    # None => not listed in addonlist.txt (game treats as enabled)
    title: str = ""
    author: str = ""
    size: int = 0
    mtime: float = 0.0
    content_flags: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def display_name(self) -> str:
        base = self.title or self.path.name
        if self.workshop_id:
            return f"{base} [{self.workshop_id}]"
        return base

    @property
    def is_enabled(self) -> bool:
        return self.enabled is not False


def quick_hash(path: Path) -> str:
    """
    Cheap identity for a VPK: size + sha1 of header and directory tree.  The
    tree contains every file's CRC32, so any content change alters the hash
    without reading gigabytes of data.
    """
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            head = f.read(28)
            h.update(head)
            if len(head) >= 12 and struct.unpack_from("<I", head, 0)[0] == 0x55AA1234:
                version, tree_size = struct.unpack_from("<II", head, 4)
                hdr = 12 if version == 1 else 28
                f.seek(hdr)
                remaining = tree_size
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
        h.update(str(path.stat().st_size).encode())
    except OSError:
        return ""
    return h.hexdigest()


def read_addoninfo(vpk: VPK) -> Tuple[str, str, List[str]]:
    entry = vpk.find("addoninfo.txt")
    if not entry:
        return "", "", []
    try:
        kv = parse_keyvalues(decode_text(vpk.read(entry)))
    except Exception:
        return "", "", []
    info = kv_get(kv, "AddonInfo") or {}
    if not isinstance(info, dict):
        return "", "", []
    title = str(kv_get(info, "addontitle", "") or "")
    author = str(kv_get(info, "addonauthor", "") or "")
    flags = [k[len("addonContent_"):] for k, v in info.items()
             if k.lower().startswith("addoncontent_") and str(v).strip() not in ("0", "")]
    return title, author, flags


class GameInstall:
    """Paths of one L4D2 installation plus helpers to enumerate its add-ons."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.game_dir = self.root / "left4dead2"
        self.addons_dir = self.game_dir / "addons"
        self.workshop_dir = self.addons_dir / "workshop"
        self.addonlist_path = self.game_dir / "addonlist.txt"
        self.cfg_dir = self.game_dir / "cfg"
        self.console_log = self.game_dir / "console.log"
        self._base_vpks: Optional[List[VPK]] = None

    # ------------------------------------------------------------- addonlist
    def read_addonlist(self) -> List[Tuple[str, str]]:
        if not self.addonlist_path.exists():
            return []
        return parse_addonlist(decode_text(self.addonlist_path.read_bytes()))

    def addonlist_map(self) -> Dict[str, str]:
        return {k.replace("/", "\\").lower(): v for k, v in self.read_addonlist()}

    # -------------------------------------------------------------- add-ons
    def list_addons(self, include_disabled: bool = True, with_info: bool = True) -> List[AddonInfo]:
        states = self.addonlist_map()
        out: List[AddonInfo] = []
        files: List[Tuple[Path, bool]] = []
        if self.addons_dir.is_dir():
            for p in sorted(self.addons_dir.glob("*.vpk")):
                files.append((p, False))
        if self.workshop_dir.is_dir():
            for p in sorted(self.workshop_dir.glob("*.vpk")):
                files.append((p, True))
        for p, is_ws in files:
            entry = f"workshop\\{p.name}" if is_ws else p.name
            state = states.get(entry.lower())
            enabled = None if state is None else (state == "1")
            if not include_disabled and enabled is False:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            info = AddonInfo(
                path=p, entry=entry, is_workshop=is_ws,
                workshop_id=p.stem if is_ws and p.stem.isdigit() else None,
                enabled=enabled, size=st.st_size, mtime=st.st_mtime,
            )
            if with_info:
                try:
                    with VPK(p) as v:
                        info.title, info.author, info.content_flags = read_addoninfo(v)
                except (VPKError, OSError) as e:
                    info.error = str(e)
            out.append(info)
        return out

    # ------------------------------------------------------------ base game
    def base_vpks(self) -> List[VPK]:
        """pak01_dir.vpk of every base content folder, in engine search order."""
        if self._base_vpks is None:
            vpks: List[VPK] = []
            for d in BASE_GAME_DIRS:
                p = self.root / d / "pak01_dir.vpk"
                if p.exists():
                    try:
                        vpks.append(VPK(p))
                    except VPKError:
                        pass
            self._base_vpks = vpks
        return self._base_vpks

    def read_base_file(self, path: str) -> Optional[bytes]:
        for v in self.base_vpks():
            e = v.find(path)
            if e:
                return v.read(e)
        # loose files on disk (left4dead2/materials/... etc.)
        for d in BASE_GAME_DIRS:
            p = self.root / d / Path(path)
            if p.is_file():
                try:
                    return p.read_bytes()
                except OSError:
                    pass
        return None

    def base_has(self, path: str) -> bool:
        return any(v.find(path) for v in self.base_vpks()) or any(
            (self.root / d / Path(path)).is_file() for d in BASE_GAME_DIRS
        )

    def close(self) -> None:
        if self._base_vpks:
            for v in self._base_vpks:
                v.close()
        self._base_vpks = None

    # ----------------------------------------------------------------- misc
    def has_condebug_log(self) -> bool:
        return self.console_log.exists()

    def crash_dumps(self) -> List[Path]:
        dumps: List[Path] = []
        for d in (self.root, self.game_dir, self.root / "bin"):
            if d.is_dir():
                dumps.extend(sorted(d.glob("*.mdmp")))
                dumps.extend(sorted(d.glob("*.dmp")))
        return dumps


def default_data_dir() -> Path:
    env = os.environ.get("IBFIX_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "L4D2IndexBufferFixer"
    return Path.home() / ".l4d2ibfix"
