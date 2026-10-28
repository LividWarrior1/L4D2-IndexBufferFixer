#!/usr/bin/env python3
"""
Read-only inspection of the L4D2 binaries that implement the index buffer
limit.  It never writes anything.  Used to ground docs/ENGINE_PATCH_FEASIBILITY.md
with facts from the installed build:

  * which DLLs contain the "Too many indices for index buffer" message,
  * how many code sites load the immediate 0x8000 (= 32768) - i.e. how many
    places a hypothetical patch would have to find and change consistently,
  * the material flag name table in materialsystem.dll ($nodecal's position
    = bit 26 = MATERIAL_VAR_SUPPRESS_DECALS).

Usage:  python tools/inspect_engine_dlls.py "C:\\...\\Left 4 Dead 2"
"""
import hashlib
import re
import struct
import sys
from pathlib import Path

NEEDLES = [
    b"Too many indices for index buffer", b"too many indices for index buffer",
    b"Too many vertices for vertex buffer", b"CIndexBufferDx8", b"CMeshDX8::LockIndexBuffer",
    b"$nodecal", b"$halflambert", b"$decal",
]
IMMEDIATES = {
    "push 0x8000": b"\x68\x00\x80\x00\x00",
    "mov eax,0x8000": b"\xb8\x00\x80\x00\x00",
    "cmp eax,0x8000": b"\x3d\x00\x80\x00\x00",
    "cmp ecx,0x8000": b"\x81\xf9\x00\x80\x00\x00",
    "cmp esi,0x8000": b"\x81\xfe\x00\x80\x00\x00",
    "cmp edi,0x8000": b"\x81\xff\x00\x80\x00\x00",
}


def main(root: str) -> int:
    bin_dir = Path(root) / "bin"
    for name in ("shaderapidx9.dll", "shaderapivk.dll", "materialsystem.dll", "studiorender.dll", "engine.dll"):
        p = bin_dir / name
        if not p.exists():
            print(f"== {name}: not found")
            continue
        d = p.read_bytes()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        machine = struct.unpack_from("<H", d, pe + 4)[0]
        arch = {0x14C: "x86 (32-bit)", 0x8664: "x64"}.get(machine, hex(machine))
        print(f"== {name}: {len(d):,} bytes, {arch}, sha1 {hashlib.sha1(d).hexdigest()[:12]}")
        for label, pat in IMMEDIATES.items():
            n = len(re.findall(re.escape(pat), d))
            if n:
                print(f"   {label:<16} {n} site(s)")
        for s in NEEDLES:
            hits = [hex(m.start()) for m in re.finditer(re.escape(s), d)]
            if hits:
                print(f"   {s.decode():<40} at {', '.join(hits[:4])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
