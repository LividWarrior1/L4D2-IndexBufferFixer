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
#
# Order of trust:
#   1. --game-dir / L4D2_DIR (normalised: quotes, trailing slashes, and paths
#      that point a level too high or too low are all accepted)
#   2. Steam libraries that contain appmanifest_550.acf - Steam's own record of
#      where L4D2 is installed.  These are checked before any other library.
#   3. Other Steam libraries, then a scan of every fixed drive.
#
# A folder only counts as an install if left4dead2/pak01_dir.vpk exists.  When
# a game is moved to another drive or uninstalled, Steam leaves user files
# (addons, cfg, saves) behind, so "has a left4dead2 folder" is not enough:
# that leftover shell is exactly what made 0.1.0 pick C: and find nothing.

_DRIVE_FIXED = 3
_SKIP_TOP_DIRS = {
    "windows", "$recycle.bin", "system volume information", "programdata", "recovery",
    "perflogs", "$windows.~bt", "$windows.~ws", "msocache", "config.msi", "$sysreset",
    "windows.old", "intel", "amd", "nvidia",
}
_COMMON_LIBRARY_DIRS = (
    "Program Files (x86)/Steam", "Program Files/Steam", "SteamLibrary", "Steam",
    "Steam Library", "Games/Steam", "Games/SteamLibrary", "Games/Steam Library",
)


@dataclass
class InstallCandidate:
    path: Path
    source: str        # where the candidate came from, shown to the user
    ok: bool = False
    reason: str = ""   # why it was rejected (or "ok")


def check_install(root: Path) -> Tuple[bool, str]:
    """Is ``root`` (the folder holding left4dead2/) a real L4D2 installation?"""
    try:
        if not root.is_dir():
            return False, "folder does not exist"
        gd = root / "left4dead2"
        if not gd.is_dir():
            return False, "no left4dead2 subfolder"
        if not (gd / "pak01_dir.vpk").is_file():
            return False, ("left4dead2 has no pak01_dir.vpk - leftover files from a moved or "
                           "uninstalled copy, not the game")
    except OSError as e:
        return False, f"cannot read folder ({e})"
    return True, "ok"


def normalize_user_path(raw: str) -> Path:
    r"""
    Clean a path typed or pasted by a user:
    * surrounding quotes from Explorer's "Copy as path",
    * a stray trailing quote from cmd.exe ("C:\...\Left 4 Dead 2\" passes the
      quote through as a literal character),
    * trailing slashes, %VARIABLES% and ~.
    """
    s = (raw or "").strip()
    if '"' in s:
        s = s.replace('"', "")
    s = s.strip().strip("'").strip()
    if len(s) > 3:
        s = s.rstrip("\\/")
    if len(s) == 2 and s[1] == ":":
        s += "\\"
    return Path(os.path.expanduser(os.path.expandvars(s)))


def roots_for_user_path(p: Path) -> List[Path]:
    """
    Every game root a user-supplied path could reasonably mean, most likely
    first: the game folder itself, a folder inside it (left4dead2, addons,
    addons/workshop, bin), a file inside it (left4dead2.exe, pak01_dir.vpk,
    addonlist.txt), or a folder above it (steamapps/common, steamapps, the
    Steam library root).
    """
    out: List[Path] = []
    try:
        if p.is_file():
            name = p.name.lower()
            p = p.parent
            if name in ("pak01_dir.vpk", "addonlist.txt", "gameinfo.txt", "console.log"):
                p = p.parent
    except OSError:
        pass
    out.append(p)
    low = p.name.lower()
    if low in ("left4dead2", "bin", "left4dead2_dlc1", "left4dead2_dlc2", "left4dead2_dlc3"):
        out.append(p.parent)
    if low == "addons":
        out.append(p.parent.parent)
    if low == "workshop" and p.parent.name.lower() == "addons":
        out.append(p.parent.parent.parent)
    if low == "cfg":
        out.append(p.parent.parent)
    out.append(p / GAME_FOLDER_NAME)                            # ...\steamapps\common
    out.append(p / "common" / GAME_FOLDER_NAME)                 # ...\steamapps
    out.append(p / "steamapps" / "common" / GAME_FOLDER_NAME)   # Steam library root
    return out


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
                            if p.is_dir():
                                return p
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        return None
    return None


def steam_roots() -> List[Path]:
    """Steam client folders on this machine (registry on Windows, usual paths elsewhere)."""
    roots: List[Path] = []
    r = _steam_root_from_registry()
    if r:
        roots.append(r)
    if sys.platform != "win32":
        home = Path.home()
        for p in (home / ".steam" / "steam", home / ".steam" / "root", home / ".local" / "share" / "Steam",
                  home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"):
            if p.is_dir():
                roots.append(p)
    return roots


def _read_vdf(path: Path) -> Dict[str, object]:
    return parse_keyvalues(decode_text(path.read_bytes()), unescape=True)


def library_folders(steam_root: Path) -> List[Path]:
    """All Steam library folders listed by a Steam client (both vdf locations, old and new format)."""
    libs: List[Path] = [steam_root]
    for vdf in (steam_root / "steamapps" / "libraryfolders.vdf", steam_root / "config" / "libraryfolders.vdf"):
        try:
            if not vdf.is_file():
                continue
            kv = _read_vdf(vdf)
        except Exception:
            continue
        lf = kv_get(kv, "libraryfolders")
        if not isinstance(lf, dict):
            continue
        for k, v in lf.items():
            path = None
            if isinstance(v, dict):
                path = kv_get(v, "path")
            elif isinstance(v, str) and k.strip().isdigit():
                path = v                       # pre-2021 format: "1"  "D:\\SteamLibrary"
            if path:
                p = Path(str(path))
                if all(os.path.normcase(str(p)) != os.path.normcase(str(x)) for x in libs):
                    libs.append(p)
    return libs


def _manifest_installdir(lib: Path) -> Optional[str]:
    """installdir from appmanifest_550.acf if this library has one, else None."""
    manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
    try:
        if not manifest.is_file():
            return None
        state = kv_get(_read_vdf(manifest), "AppState") or {}
        if isinstance(state, dict):
            return str(kv_get(state, "installdir", GAME_FOLDER_NAME) or GAME_FOLDER_NAME)
    except Exception:
        pass
    return GAME_FOLDER_NAME


def fixed_drives() -> List[Path]:
    """Local fixed drives (never floppy/card-reader/optical, which can pop 'insert disk' dialogs)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        import string

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = k32.GetLogicalDrives()
        out = []
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == _DRIVE_FIXED:
                    out.append(Path(root))
        return out
    except Exception:
        return [Path(f"{l}:\\") for l in "CDEFGHIJ" if os.path.isdir(f"{l}:\\")]


def _is_dir(p: Path) -> bool:
    """Path.is_dir() that never raises: on Windows it throws PermissionError for
    protected folders (System Volume Information, other users' profiles...)."""
    try:
        return p.is_dir()
    except OSError:
        return False


def _subdirs(p: Path, skip: frozenset = frozenset()) -> List[Path]:
    """Child folders of p; unreadable entries are skipped one by one, never the whole listing."""
    out: List[Path] = []
    try:
        entries = list(p.iterdir())
    except OSError:
        return out
    for e in entries:
        if e.name.lower() in skip:
            continue
        if _is_dir(e):
            out.append(e)
    return out


def _drive_scan(drive_roots: Iterable[Path]) -> Iterable[Tuple[Path, str]]:
    skip = frozenset(_SKIP_TOP_DIRS)
    for d in drive_roots:
        for rel in _COMMON_LIBRARY_DIRS:
            lib = d / rel
            yield lib / "steamapps" / "common" / GAME_FOLDER_NAME, f"drive scan: {lib}"
        # any folder up to two levels deep that holds a steamapps folder
        for t in _subdirs(d, skip):
            for lib in [t] + _subdirs(t):
                if _is_dir(lib / "steamapps"):
                    yield lib / "steamapps" / "common" / GAME_FOLDER_NAME, f"drive scan: {lib}"


def find_installs(explicit: Optional[str] = None, *, exhaustive: bool = False,
                  steam_root_list: Optional[List[Path]] = None,
                  drive_roots: Optional[List[Path]] = None) -> List[InstallCandidate]:
    """
    Every place checked, in order, with a verdict.  The first ``ok`` entry is
    the installation to use.  With ``explicit`` only that path (and the levels
    around it) is considered: a typo must produce an error, not a silent
    fallback to some other copy.  ``steam_root_list`` / ``drive_roots`` exist
    so tests can point the search at a fake filesystem.
    """
    out: List[InstallCandidate] = []
    seen: set = set()

    def add(path: Path, source: str, only_if_exists: bool = False) -> None:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            return
        try:
            if only_if_exists and not path.is_dir():
                return
        except OSError:
            return
        seen.add(key)
        ok, reason = check_install(path)
        out.append(InstallCandidate(path, source, ok, reason))

    def done() -> bool:
        return not exhaustive and any(c.ok for c in out)

    if explicit:
        for c in roots_for_user_path(normalize_user_path(explicit)):
            add(c, "--game-dir", only_if_exists=bool(out))
        return out

    env = os.environ.get("L4D2_DIR")
    if env:
        for c in roots_for_user_path(normalize_user_path(env)):
            add(c, "L4D2_DIR", only_if_exists=bool(out))
        if done():
            return out

    libs: List[Path] = []
    for root in (steam_root_list if steam_root_list is not None else steam_roots()):
        for lib in library_folders(root):
            if all(os.path.normcase(str(lib)) != os.path.normcase(str(x)) for x in libs):
                libs.append(lib)
    # Libraries Steam says L4D2 is installed in come first, whatever drive they are on.
    for lib in libs:
        installdir = _manifest_installdir(lib)
        if installdir:
            add(lib / "steamapps" / "common" / installdir, f"Steam library {lib} (has appmanifest_550.acf)")
    if done():
        return out
    for lib in libs:
        add(lib / "steamapps" / "common" / GAME_FOLDER_NAME, f"Steam library {lib}", only_if_exists=True)
    if done():
        return out

    for path, source in _drive_scan(drive_roots if drive_roots is not None else fixed_drives()):
        add(path, source, only_if_exists=True)
        if done():
            return out
    if sys.platform != "win32":
        add(Path.home() / "mnt" / GAME_FOLDER_NAME, "linked-folder mount", only_if_exists=True)
    return out


def find_game_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """The folder containing left4dead2/ (with its pak01_dir.vpk), or None."""
    for c in find_installs(explicit):
        if c.ok:
            return c.path
    return None


def format_candidates(cands: List[InstallCandidate], limit: int = 15) -> str:
    lines = []
    for c in cands[:limit]:
        mark = "OK " if c.ok else "-- "
        lines.append(f"  {mark}{c.path}\n       from {c.source}" + ("" if c.ok else f": {c.reason}"))
    if len(cands) > limit:
        lines.append(f"  ... and {len(cands) - limit} more")
    return "\n".join(lines) if lines else "  (no Steam installation or library folders were found)"


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
