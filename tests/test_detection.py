"""
Game-folder detection tests (issue #1: "Assumes C drive at all times").

Each test builds a fake Steam layout in a temp folder.  Drive letters are
plain directories named C, D, ... and the search is pointed at them through
``steam_root_list`` / ``drive_roots``, so these run on any OS.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l4d2_ibfix import gamefiles as gf  # noqa: E402
from l4d2_ibfix.cli import repair_windows_quotes  # noqa: E402
from l4d2_ibfix.keyvalues import kv_get, parse_keyvalues  # noqa: E402

GAME = "Left 4 Dead 2"


def make_install(root: Path) -> Path:
    """A minimal real-looking install: <root>/left4dead2/pak01_dir.vpk + exe."""
    (root / "left4dead2" / "addons" / "workshop").mkdir(parents=True, exist_ok=True)
    (root / "left4dead2" / "pak01_dir.vpk").write_bytes(b"\x34\x12\xaa\x55\x01\x00\x00\x00\x01\x00\x00\x00\x00")
    (root / "left4dead2.exe").write_bytes(b"MZ")
    return root


def make_leftover(root: Path) -> Path:
    """What Steam leaves behind after moving/uninstalling: user files, no game data."""
    (root / "left4dead2" / "addons").mkdir(parents=True, exist_ok=True)
    (root / "left4dead2" / "cfg").mkdir(parents=True, exist_ok=True)
    (root / "left4dead2" / "cfg" / "config.cfg").write_text("// leftover")
    return root


def write_libraryfolders(steam_root: Path, libs, app_lib=None, old_format=False, where="steamapps"):
    (steam_root / where).mkdir(parents=True, exist_ok=True)
    body = '"libraryfolders"\n{\n'
    for i, lib in enumerate(libs):
        esc = str(lib).replace("\\", "\\\\")
        if old_format:
            if i:
                body += f'\t"{i}"\t\t"{esc}"\n'
            continue
        apps = '"550"\t\t"13000000000"' if lib == app_lib else '"228980"\t\t"0"'
        body += f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{esc}"\n\t\t"apps"\n\t\t{{\n\t\t\t{apps}\n\t\t}}\n\t}}\n'
    body += "}\n"
    (steam_root / where / "libraryfolders.vdf").write_text(body)


def write_manifest(lib: Path, installdir: str = GAME):
    (lib / "steamapps").mkdir(parents=True, exist_ok=True)
    (lib / "steamapps" / "appmanifest_550.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"550"\n\t"installdir"\t\t"{installdir}"\n}}\n')


def chosen(cands):
    ok = [c for c in cands if c.ok]
    return ok[0].path if ok else None


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self.C = self.base / "C"
        self.D = self.base / "D"
        self.E = self.base / "E"
        for d in (self.C, self.D, self.E):
            d.mkdir()
        self.steam = self.C / "Program Files (x86)" / "Steam"
        (self.steam / "steamapps").mkdir(parents=True)
        self._env = os.environ.pop("L4D2_DIR", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["L4D2_DIR"] = self._env
        self._td.cleanup()


class MovedGame(Base):
    """The issue-#1 situation: game moved C: -> D:, Steam left a shell on C:."""

    def setUp(self):
        super().setUp()
        self.dlib = self.D / "SteamLibrary"
        make_leftover(self.steam / "steamapps" / "common" / GAME)
        self.real = make_install(self.dlib / "steamapps" / "common" / GAME)
        write_manifest(self.dlib)
        write_libraryfolders(self.steam, [self.steam, self.dlib], app_lib=self.dlib)

    def test_picks_the_real_install_not_the_c_leftover(self):
        c = gf.find_installs(steam_root_list=[self.steam], drive_roots=[self.C, self.D])
        self.assertEqual(chosen(c), self.real)

    def test_leftover_is_reported_with_reason(self):
        c = gf.find_installs(steam_root_list=[self.steam], drive_roots=[self.C, self.D], exhaustive=True)
        left = [x for x in c if not x.ok and x.path == self.steam / "steamapps" / "common" / GAME]
        self.assertTrue(left and "pak01_dir.vpk" in left[0].reason)

    def test_manifest_library_wins_even_if_listed_last(self):
        # a second, also-valid copy on C: without a manifest must not beat the manifest one
        make_install(self.steam / "steamapps" / "common" / GAME)
        c = gf.find_installs(steam_root_list=[self.steam], drive_roots=[])
        self.assertEqual(chosen(c), self.real)

    def test_config_folder_libraryfolders(self):
        (self.steam / "steamapps" / "libraryfolders.vdf").unlink()
        write_libraryfolders(self.steam, [self.steam, self.dlib], app_lib=self.dlib, where="config")
        self.assertEqual(chosen(gf.find_installs(steam_root_list=[self.steam], drive_roots=[])), self.real)

    def test_old_libraryfolders_format(self):
        write_libraryfolders(self.steam, [self.steam, self.dlib], old_format=True)
        self.assertEqual(chosen(gf.find_installs(steam_root_list=[self.steam], drive_roots=[])), self.real)


class NoSteamInfo(Base):
    """Registry/libraryfolders unavailable: the drive scan must find any drive, not just C-F."""

    def test_drive_scan_finds_custom_library_on_another_drive(self):
        real = make_install(self.E / "Games" / "Steam" / "steamapps" / "common" / GAME)
        c = gf.find_installs(steam_root_list=[], drive_roots=[self.C, self.D, self.E])
        self.assertEqual(chosen(c), real)

    def test_drive_scan_finds_two_level_custom_folder(self):
        real = make_install(self.D / "Stuff" / "MySteam" / "steamapps" / "common" / GAME)
        c = gf.find_installs(steam_root_list=[], drive_roots=[self.C, self.D])
        self.assertEqual(chosen(c), real)

    def test_nothing_but_a_leftover_gives_no_install(self):
        make_leftover(self.C / "SteamLibrary" / "steamapps" / "common" / GAME)
        c = gf.find_installs(steam_root_list=[], drive_roots=[self.C])
        self.assertIsNone(chosen(c))
        self.assertTrue(any("leftover" in x.reason for x in c))


class ExplicitGameDir(Base):
    def setUp(self):
        super().setUp()
        self.lib = self.D / "SteamLibrary"
        self.real = make_install(self.lib / "steamapps" / "common" / GAME)

    def check(self, raw):
        self.assertEqual(chosen(gf.find_installs(str(raw))), self.real, raw)

    def test_exact(self):
        self.check(self.real)

    def test_quoted_copy_as_path(self):
        self.check(f'"{self.real}"')

    def test_cmd_trailing_backslash_quote(self):
        self.check(f'{self.real}{os.sep}"')

    def test_trailing_separator(self):
        self.check(f"{self.real}{os.sep}")

    def test_points_one_level_too_deep(self):
        for sub in ("left4dead2", "left4dead2/addons", "left4dead2/addons/workshop", "bin"):
            (self.real / sub).mkdir(parents=True, exist_ok=True)
            self.check(self.real / sub)

    def test_points_at_a_file(self):
        self.check(self.real / "left4dead2.exe")
        self.check(self.real / "left4dead2" / "pak01_dir.vpk")

    def test_points_too_high(self):
        self.check(self.lib / "steamapps" / "common")
        self.check(self.lib / "steamapps")
        self.check(self.lib)

    def test_wrong_path_does_not_silently_fall_back(self):
        # a valid install exists elsewhere, but the user named a different folder:
        # that must be an error, not a quiet switch to some other copy
        c = gf.find_installs(str(self.base / "nope"), steam_root_list=[], drive_roots=[self.D])
        self.assertIsNone(chosen(c))

    def test_env_var(self):
        os.environ["L4D2_DIR"] = f'"{self.real}"'
        self.assertEqual(chosen(gf.find_installs(steam_root_list=[], drive_roots=[])), self.real)


class Parsing(unittest.TestCase):
    def test_vdf_backslashes_unescaped(self):
        kv = parse_keyvalues('"x"\n{\n\t"path"\t\t"D:\\\\Steam Library\\\\games"\n}\n', unescape=True)
        self.assertEqual(kv_get(kv_get(kv, "x"), "path"), "D:\\Steam Library\\games")

    def test_vmt_backslashes_untouched_by_default(self):
        kv = parse_keyvalues('VertexLitGeneric\n{\n\t"$basetexture" "models\\\\x\\\\y"\n}\n')
        self.assertEqual(kv["VertexLitGeneric"]["$basetexture"], "models\\\\x\\\\y")

    def test_repair_windows_quotes(self):
        self.assertEqual(repair_windows_quotes(["--game-dir", 'D:\\G\\Left 4 Dead 2\\" scan --all']),
                         ["--game-dir", "D:\\G\\Left 4 Dead 2\\", "scan", "--all"])
        self.assertEqual(repair_windows_quotes(["scan", "-v"]), ["scan", "-v"])

    def test_normalize_drive_letter(self):
        self.assertTrue(str(gf.normalize_user_path('"D:"')).startswith("D:"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class RealWindows(unittest.TestCase):
    """Run on a real Windows machine (GitHub Actions windows-latest)."""

    def test_fixed_drives_includes_system_drive(self):
        drives = [str(d).upper() for d in gf.fixed_drives()]
        sysdrive = (os.environ.get("SystemDrive", "C:") + "\\").upper()
        self.assertIn(sysdrive, drives)

    def test_full_search_runs_without_error(self):
        gf.find_installs(exhaustive=True)   # registry + every fixed drive; must not raise or hang

    def test_cmd_exe_trailing_backslash_quote(self):
        """The literal command line a user types into cmd.exe, parsed by cmd.exe itself."""
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            real = make_install(Path(td) / "Some Drive Folder" / GAME)
            ibfix = Path(__file__).resolve().parents[1] / "ibfix.py"
            cmd = f'"{sys.executable}" "{ibfix}" --game-dir "{real}\\" locate'
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("Will use:", r.stdout)
            self.assertIn(str(real), r.stdout)
