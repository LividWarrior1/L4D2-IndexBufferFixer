"""
Unit tests.  Run:  python -m pytest tests  (or  python -m unittest discover tests)

Synthetic tests always run.  Tests that need real game files run only when
L4D2_DIR points at an installation (or the auto-detected one exists).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from l4d2_ibfix import keyvalues as kv  # noqa: E402
from l4d2_ibfix.vpk import VPK, copy_vpk_with_replacements, write_vpk_v1  # noqa: E402
from l4d2_ibfix.gamefiles import GameInstall, find_game_dir  # noqa: E402
from l4d2_ibfix.mdl import analyze_model, companion_paths, parse_mdl, parse_vtx, parse_vvd  # noqa: E402


class VPKRoundTrip(unittest.TestCase):
    def test_write_read(self):
        files = {
            "addoninfo.txt": b'"AddonInfo"\n{\n\taddontitle "t"\n}\n',
            "materials/models/x/a.vmt": b'VertexLitGeneric\n{\n\t"$basetexture" "x/a"\n}\n',
            "materials/models/x/a.vtf": bytes(range(256)) * 4,
            "models/foo.mdl": b"IDST" + b"\x00" * 500,
            "README": b"no extension",
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.vpk"
            write_vpk_v1(p, files)
            with VPK(p) as v:
                self.assertEqual(v.version, 1)
                self.assertEqual(set(v.entries), set(files))
                for k, data in files.items():
                    self.assertEqual(v.read(k, verify_crc=True), data)
                self.assertIsNotNone(v.find("MATERIALS/MODELS/X/A.VMT"))
                # copy with replacement
                q = Path(td) / "u.vpk"
                copy_vpk_with_replacements(v, q, {"materials/models/x/a.vmt": b"patched"}, {"new/file.txt": b"n"})
            with VPK(q) as w:
                self.assertEqual(w.read("materials/models/x/a.vmt", verify_crc=True), b"patched")
                self.assertEqual(w.read("new/file.txt", verify_crc=True), b"n")
                self.assertEqual(w.read("materials/models/x/a.vtf", verify_crc=True), files["materials/models/x/a.vtf"])
                self.assertEqual(len(w), len(files) + 1)


class VMTPatch(unittest.TestCase):
    def test_add(self):
        t = 'VertexLitGeneric\n{\n\t"$basetexture" "a"\n}\n'
        n, a = kv.patch_vmt_nodecal(t)
        self.assertEqual(a, "added")
        self.assertIn('"$nodecal" "1"', n)
        self.assertEqual(kv.patch_vmt_nodecal(n)[1], "unchanged")

    def test_flip_zero(self):
        n, a = kv.patch_vmt_nodecal('X { $nodecal 0 }')
        self.assertEqual(a, "changed")
        self.assertIn('"$nodecal" "1"', n)

    def test_patch_shader_insert(self):
        n, a = kv.patch_vmt_nodecal('patch\n{\n\tinclude "a.vmt"\n\tinsert\n\t{\n\t\t"$x" "1"\n\t}\n}\n')
        self.assertEqual(a, "added")
        self.assertLess(n.index("insert"), n.index("$nodecal"))

    def test_patch_shader_no_insert(self):
        n, a = kv.patch_vmt_nodecal('patch\n{\n\tinclude "a.vmt"\n}\n')
        self.assertEqual(a, "added")
        self.assertIn("insert", n)
        d = kv.parse_keyvalues(n)
        self.assertEqual(kv.kv_get(kv.kv_get(d, "patch"), "insert")["$nodecal"], "1")

    def test_crlf_and_bom(self):
        t = '﻿"VertexLitGeneric"\r\n{\r\n\t"$basetexture" "a"\r\n}\r\n'
        n, a = kv.patch_vmt_nodecal(t)
        self.assertEqual(a, "added")
        self.assertTrue(n.startswith("﻿"))
        self.assertIn('\r\n\t"$nodecal" "1"\r\n', n)

    def test_comment_brace(self):
        n, a = kv.patch_vmt_nodecal('// { not a block\nX\n{\n  $a 1 [$X360]\n}\n')
        self.assertEqual(a, "added")
        self.assertTrue(kv.parse_keyvalues(n)["X"]["$nodecal"] == "1")

    def test_empty(self):
        self.assertEqual(kv.patch_vmt_nodecal("   ")[1], "skipped")


class AddonList(unittest.TestCase):
    TEXT = '"AddonList"\n{\n\t"workshop\\123.vpk"\t\t"1"\n\t"local.vpk"\t\t"0"\n}\n'

    def test_parse(self):
        self.assertEqual(kv.parse_addonlist(self.TEXT), [("workshop\\123.vpk", "1"), ("local.vpk", "0")])

    def test_set_and_append(self):
        new, prev = kv.set_addonlist_values(self.TEXT, {"workshop/123.vpk": "0", "ibfix_x.vpk": "1"})
        self.assertEqual(prev, {"workshop\\123.vpk": "1"})
        entries = dict(kv.parse_addonlist(new))
        self.assertEqual(entries["workshop\\123.vpk"], "0")
        self.assertEqual(entries["ibfix_x.vpk"], "1")
        self.assertTrue(new.rstrip().endswith("}"))


def _game():
    root = find_game_dir(os.environ.get("L4D2_DIR"))
    return GameInstall(root) if root else None


@unittest.skipUnless(_game() and _game().base_vpks(), "needs a Left 4 Dead 2 installation")
class RealFiles(unittest.TestCase):
    def test_stock_survivor(self):
        g = _game()
        p = "models/survivors/survivor_coach.mdl"
        mdl = parse_mdl(g.read_base_file(p), p)
        vtxp, vvdp = companion_paths(p)
        vtx = parse_vtx(g.read_base_file(vtxp), vtxp, mdl.version)
        vvd = parse_vvd(g.read_base_file(vvdp), vvdp)
        st = analyze_model(mdl, vtx, vvd)
        self.assertTrue(st.checksum_ok)
        self.assertTrue(10000 < st.lod0_triangles < 30000, st.lod0_triangles)
        self.assertTrue(st.total_area > 1000)
        self.assertTrue(any("coach_body" in m.texture_names[0] for m in st.materials))

    def test_addons_parse(self):
        g = _game()
        for a in g.list_addons(with_info=True)[:50]:
            self.assertEqual(a.error, "", a.path)


if __name__ == "__main__":
    unittest.main()
