"""
Pure-Python reader/writer for Valve VPK archives.

Left 4 Dead 2 uses VPK **version 1** everywhere:

* ``left4dead2/pak01_dir.vpk`` is a *directory* VPK whose file data lives in
  numbered archives ``pak01_000.vpk`` ... ``pak01_NNN.vpk``.
* Workshop and local add-ons are *single-file* VPKs: the directory tree is
  followed immediately by the file data (archive index ``0x7FFF``).

Version 2 (used by TF2 / CS:GO) is also readable so the code is reusable, but
the writer only ever emits version 1 single-file archives, which is exactly
what ``bin/vpk.exe`` produces for L4D2 add-ons.

Format reference: https://developer.valvesoftware.com/wiki/VPK_(file_format)
"""
from __future__ import annotations

import io
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Tuple, Union

VPK_SIGNATURE = 0x55AA1234
DIR_ARCHIVE_INDEX = 0x7FFF  # data is stored in the _dir / single file itself
ENTRY_TERMINATOR = 0xFFFF


class VPKError(Exception):
    """Raised for malformed or unsupported VPK data."""


@dataclass(frozen=True)
class VPKEntry:
    path: str            # normalised, forward slashes, e.g. "materials/models/x.vmt"
    crc32: int
    preload: bytes       # bytes stored inline in the directory (usually empty)
    archive_index: int   # 0x7FFF => in directory file
    offset: int          # relative to data section start (dir file) or archive start
    length: int          # bytes stored in the archive (excludes preload)

    @property
    def size(self) -> int:
        return len(self.preload) + self.length


def _read_cstring(buf: bytes, pos: int) -> Tuple[str, int]:
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", errors="replace"), end + 1


def normalize_path(p: str) -> str:
    """Forward slashes, no leading slash, no './' segments."""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


class VPK:
    """
    Read-only view of a VPK.  Open with :meth:`open` and iterate over
    :attr:`entries`.  File data is read lazily, so opening a 1 GB add-on just
    parses the (small) directory tree.
    """

    def __init__(self, path: Union[str, os.PathLike]):
        self.path = Path(path)
        self.version = 0
        self.tree_size = 0
        self.header_size = 0
        self.entries: Dict[str, VPKEntry] = {}
        self._archive_cache: Dict[int, io.BufferedReader] = {}
        self._parse()

    # ------------------------------------------------------------------ parse
    def _parse(self) -> None:
        with open(self.path, "rb") as f:
            head = f.read(28)
            if len(head) < 12:
                raise VPKError(f"{self.path}: file too small to be a VPK")
            sig, version, tree_size = struct.unpack_from("<III", head, 0)
            if sig != VPK_SIGNATURE:
                raise VPKError(f"{self.path}: bad signature 0x{sig:08X} (not a VPK)")
            if version == 1:
                self.header_size = 12
            elif version == 2:
                if len(head) < 28:
                    raise VPKError(f"{self.path}: truncated v2 header")
                self.header_size = 28
            else:
                raise VPKError(f"{self.path}: unsupported VPK version {version}")
            self.version = version
            self.tree_size = tree_size
            f.seek(self.header_size)
            tree = f.read(tree_size)
            if len(tree) != tree_size:
                raise VPKError(f"{self.path}: truncated directory tree")
        self._parse_tree(tree)

    def _parse_tree(self, tree: bytes) -> None:
        pos = 0
        entries: Dict[str, VPKEntry] = {}
        while True:
            ext, pos = _read_cstring(tree, pos)
            if ext == "":
                break
            while True:
                directory, pos = _read_cstring(tree, pos)
                if directory == "":
                    break
                while True:
                    name, pos = _read_cstring(tree, pos)
                    if name == "":
                        break
                    crc, preload_len, archive_index, offset, length, term = struct.unpack_from(
                        "<IHHIIH", tree, pos
                    )
                    pos += 18
                    if term != ENTRY_TERMINATOR:
                        raise VPKError(f"{self.path}: bad entry terminator for {directory}/{name}.{ext}")
                    preload = tree[pos:pos + preload_len]
                    pos += preload_len
                    if directory in (" ", ""):
                        full = f"{name}.{ext}" if ext != " " else name
                    else:
                        full = f"{directory}/{name}.{ext}" if ext != " " else f"{directory}/{name}"
                    full = normalize_path(full)
                    entries[full] = VPKEntry(full, crc, preload, archive_index, offset, length)
        self.entries = entries

    # ------------------------------------------------------------------- api
    def __contains__(self, path: str) -> bool:
        return normalize_path(path).lower() in self._lower_index()

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    _lower: Optional[Dict[str, str]] = None

    def _lower_index(self) -> Dict[str, str]:
        if self._lower is None:
            self._lower = {k.lower(): k for k in self.entries}
        return self._lower

    def find(self, path: str) -> Optional[VPKEntry]:
        """Case-insensitive lookup (Source's filesystem is case-insensitive on Windows)."""
        key = self._lower_index().get(normalize_path(path).lower())
        return self.entries[key] if key else None

    def _archive_path(self, index: int) -> Path:
        if index == DIR_ARCHIVE_INDEX:
            return self.path
        stem = self.path.name
        if stem.lower().endswith("_dir.vpk"):
            base = stem[:-8]
        else:
            raise VPKError(f"{self.path}: entry references archive {index} but this is not a _dir.vpk")
        return self.path.with_name(f"{base}_{index:03d}.vpk")

    def _archive(self, index: int) -> io.BufferedReader:
        f = self._archive_cache.get(index)
        if f is None:
            p = self._archive_path(index)
            if not p.exists():
                raise VPKError(f"missing archive {p}")
            f = open(p, "rb")
            self._archive_cache[index] = f
        return f

    def read(self, path_or_entry: Union[str, VPKEntry], verify_crc: bool = False) -> bytes:
        entry = path_or_entry if isinstance(path_or_entry, VPKEntry) else self.find(path_or_entry)
        if entry is None:
            raise KeyError(path_or_entry)
        data = entry.preload
        if entry.length:
            f = self._archive(entry.archive_index)
            if entry.archive_index == DIR_ARCHIVE_INDEX:
                f.seek(self.header_size + self.tree_size + entry.offset)
            else:
                f.seek(entry.offset)
            chunk = f.read(entry.length)
            if len(chunk) != entry.length:
                raise VPKError(f"{self.path}: short read for {entry.path}")
            data = data + chunk
        if verify_crc and (zlib.crc32(data) & 0xFFFFFFFF) != entry.crc32:
            raise VPKError(f"{self.path}: CRC mismatch for {entry.path}")
        return data

    def close(self) -> None:
        for f in self._archive_cache.values():
            try:
                f.close()
            except OSError:
                pass
        self._archive_cache.clear()

    def __enter__(self) -> "VPK":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # convenience -------------------------------------------------------------
    def paths_with_suffix(self, *suffixes: str) -> Iterable[str]:
        sfx = tuple(s.lower() for s in suffixes)
        for p in self.entries:
            if p.lower().endswith(sfx):
                yield p

    def is_single_file(self) -> bool:
        return all(e.archive_index == DIR_ARCHIVE_INDEX for e in self.entries.values())


# --------------------------------------------------------------------------- write
def _split_path(full: str) -> Tuple[str, str, str]:
    """'materials/models/foo.vmt' -> ('vmt', 'materials/models', 'foo')."""
    full = normalize_path(full)
    directory, _, filename = full.rpartition("/")
    if "." in filename:
        name, _, ext = filename.rpartition(".")
        if name == "":
            # dotfile such as ".gitignore": treat whole thing as name
            name, ext = filename, " "
    else:
        name, ext = filename, " "
    return ext, (directory if directory else " "), name


def write_vpk_v1(out_path: Union[str, os.PathLike], files: Dict[str, bytes]) -> None:
    """
    Write a single-file VPK v1 (the format of every L4D2 add-on).

    ``files`` maps archive paths (forward or back slashes) to their bytes.
    Entries are grouped by extension -> directory -> filename exactly like
    Valve's ``vpk.exe``; all data is appended after the tree with
    ``ArchiveIndex = 0x7FFF`` and zero preload bytes.
    """
    # Group ext -> dir -> name -> data
    grouped: Dict[str, Dict[str, Dict[str, bytes]]] = {}
    for full, data in files.items():
        ext, directory, name = _split_path(full)
        grouped.setdefault(ext, {}).setdefault(directory, {})[name] = data

    tree = io.BytesIO()
    body = io.BytesIO()
    for ext in sorted(grouped):
        tree.write(ext.encode("utf-8") + b"\x00")
        for directory in sorted(grouped[ext]):
            tree.write(directory.encode("utf-8") + b"\x00")
            for name in sorted(grouped[ext][directory]):
                data = grouped[ext][directory][name]
                crc = zlib.crc32(data) & 0xFFFFFFFF
                tree.write(name.encode("utf-8") + b"\x00")
                tree.write(struct.pack("<IHHIIH", crc, 0, DIR_ARCHIVE_INDEX, body.tell(), len(data), ENTRY_TERMINATOR))
                body.write(data)
            tree.write(b"\x00")
        tree.write(b"\x00")
    tree.write(b"\x00")

    tree_bytes = tree.getvalue()
    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", VPK_SIGNATURE, 1, len(tree_bytes)))
        f.write(tree_bytes)
        f.write(body.getvalue())
    os.replace(tmp, out_path)


def copy_vpk_with_replacements(
    src: VPK,
    out_path: Union[str, os.PathLike],
    replacements: Dict[str, bytes],
    additions: Optional[Dict[str, bytes]] = None,
) -> None:
    """
    Re-pack ``src`` into a new single-file v1 VPK, substituting the bytes of
    any path in ``replacements`` (case-insensitive match) and adding
    ``additions``.  The source VPK is never modified.

    Data is streamed entry-by-entry so a 1 GB add-on does not need to fit in
    memory; only the directory tree is held in RAM.
    """
    repl_lower = {normalize_path(k).lower(): v for k, v in replacements.items()}
    additions = {normalize_path(k): v for k, v in (additions or {}).items()}

    # Build ordered list of (ext, dir, name, source) where source is bytes or entry
    grouped: Dict[str, Dict[str, Dict[str, Union[bytes, VPKEntry]]]] = {}
    for full, entry in src.entries.items():
        ext, directory, name = _split_path(full)
        data: Union[bytes, VPKEntry] = repl_lower.get(full.lower(), entry)
        grouped.setdefault(ext, {}).setdefault(directory, {})[name] = data
    for full, data in additions.items():
        ext, directory, name = _split_path(full)
        grouped.setdefault(ext, {}).setdefault(directory, {})[name] = data

    # First pass: compute sizes/CRCs to lay out the tree without buffering bodies.
    layout = []  # (ext, dir, name, source, crc, length)
    offset = 0
    for ext in sorted(grouped):
        for directory in sorted(grouped[ext]):
            for name in sorted(grouped[ext][directory]):
                source = grouped[ext][directory][name]
                if isinstance(source, VPKEntry):
                    length = source.size
                    crc = source.crc32
                    if source.preload:
                        # preload data gets folded into the body; recompute CRC later on write
                        crc = None
                else:
                    length = len(source)
                    crc = zlib.crc32(source) & 0xFFFFFFFF
                layout.append((ext, directory, name, source, crc, length, offset))
                offset += length

    # Resolve any CRCs we could not take from the directory (preload entries).
    resolved = []
    for ext, directory, name, source, crc, length, off in layout:
        if crc is None:
            data = src.read(source)
            crc = zlib.crc32(data) & 0xFFFFFFFF
            source = data
        resolved.append((ext, directory, name, source, crc, length, off))

    tree = io.BytesIO()
    cur_ext = cur_dir = None
    for ext, directory, name, source, crc, length, off in resolved:
        if ext != cur_ext:
            if cur_ext is not None:
                tree.write(b"\x00\x00")  # close dir + ext
            tree.write(ext.encode("utf-8") + b"\x00")
            cur_ext, cur_dir = ext, None
        if directory != cur_dir:
            if cur_dir is not None:
                tree.write(b"\x00")
            tree.write(directory.encode("utf-8") + b"\x00")
            cur_dir = directory
        tree.write(name.encode("utf-8") + b"\x00")
        tree.write(struct.pack("<IHHIIH", crc, 0, DIR_ARCHIVE_INDEX, off, length, ENTRY_TERMINATOR))
    if cur_ext is not None:
        tree.write(b"\x00\x00")
    tree.write(b"\x00")
    tree_bytes = tree.getvalue()

    out_path = Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", VPK_SIGNATURE, 1, len(tree_bytes)))
        f.write(tree_bytes)
        for ext, directory, name, source, crc, length, off in resolved:
            if isinstance(source, VPKEntry):
                f.write(src.read(source))
            else:
                f.write(source)
    os.replace(tmp, out_path)
