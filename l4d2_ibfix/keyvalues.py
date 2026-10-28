"""
Tolerant Valve KeyValues helpers.

Two jobs:

1. ``parse_keyvalues`` - a small recursive parser good enough for
   ``addoninfo.txt``, ``addonlist.txt`` and ``.vmt`` files (comments,
   quoted/unquoted tokens, ``[$conditionals]``, ``#include``/``#base`` lines
   are tolerated and ignored).

2. ``patch_vmt_nodecal`` - *textual* insertion of ``"$nodecal" "1"`` into a
   VMT.  We deliberately do not re-serialise the file from a parsed tree: the
   original bytes (comments, ordering, odd spacing) are preserved exactly,
   and only one line is added or changed.  That keeps the change reviewable
   and trivially reversible.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Union

KV = Dict[str, Union[str, "KV", List]]  # type: ignore


class KeyValuesError(Exception):
    pass


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*)
  | (?P<lbrace>\{)
  | (?P<rbrace>\})
  | (?P<cond>\[[^\]\n]*\])
  | "(?P<quoted>(?:[^"\\]|\\.)*)"
  | (?P<bare>[^\s{}"\[\]]+)
    """,
    re.VERBOSE,
)


def tokenize(text: str) -> List[Tuple[str, str, int, int]]:
    """Return (kind, value, start, end) tuples; whitespace and comments are dropped."""
    out = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            # unknown byte (e.g. stray ']'), skip it
            pos += 1
            continue
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            pos = m.end()
            continue
        val = m.group(kind)
        out.append((kind, val, m.start(), m.end()))
        pos = m.end()
    return out


def parse_keyvalues(text: str) -> Dict[str, object]:
    """
    Parse into nested dicts.  Duplicate keys are kept as a list under the key.
    Keys are returned as written (callers usually lower() them).
    """
    if text.startswith("﻿"):
        text = text[1:]
    toks = tokenize(text)
    i = 0

    def parse_block() -> Dict[str, object]:
        nonlocal i
        d: Dict[str, object] = {}
        while i < len(toks):
            kind, val, _s, _e = toks[i]
            if kind == "rbrace":
                i += 1
                return d
            if kind in ("quoted", "bare"):
                key = val
                i += 1
                # skip conditionals between key and value
                while i < len(toks) and toks[i][0] == "cond":
                    i += 1
                if i >= len(toks):
                    d[key] = ""
                    break
                k2, v2, _s2, _e2 = toks[i]
                if k2 == "lbrace":
                    i += 1
                    value: object = parse_block()
                elif k2 in ("quoted", "bare"):
                    value = v2
                    i += 1
                elif k2 == "rbrace":
                    value = ""
                else:
                    i += 1
                    continue
                # trailing conditional
                while i < len(toks) and toks[i][0] == "cond":
                    i += 1
                if key in d:
                    prev = d[key]
                    if isinstance(prev, list):
                        prev.append(value)
                    else:
                        d[key] = [prev, value]
                else:
                    d[key] = value
            else:
                i += 1
        return d

    return parse_block()


def kv_get(d: Dict[str, object], key: str, default=None):
    """Case-insensitive lookup; returns the first value if duplicates exist."""
    kl = key.lower()
    for k, v in d.items():
        if k.lower() == kl:
            return v[0] if isinstance(v, list) and v else v
    return default


# ------------------------------------------------------------------ VMT patch
def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _block_indent(text: str, lbrace_end: int) -> str:
    """Indentation used by the first key line inside a block, default a tab."""
    m = re.compile(r"\r?\n([ \t]*)\S").search(text, lbrace_end)
    if m:
        return m.group(1) or "\t"
    return "\t"


def vmt_shader_name(text: str) -> Optional[str]:
    toks = tokenize(text.lstrip("﻿"))
    for kind, val, _s, _e in toks:
        if kind in ("quoted", "bare"):
            return val
        if kind == "lbrace":
            return None
    return None


def find_nodecal(text: str) -> Optional[Tuple[int, int, str]]:
    """Locate an existing $nodecal key at any depth: returns (start, end, value)."""
    toks = tokenize(text)
    for n, (kind, val, s, e) in enumerate(toks):
        if kind in ("quoted", "bare") and val.lower() == "$nodecal":
            # value is the next quoted/bare token
            for k2, v2, s2, e2 in toks[n + 1:n + 4]:
                if k2 in ("quoted", "bare"):
                    return s, e2, v2
                if k2 == "cond":
                    continue
                break
            return s, e, ""
    return None


def patch_vmt_nodecal(text: str) -> Tuple[str, str]:
    """
    Return (new_text, action) where action is one of:
      'added'      - a "$nodecal" "1" line was inserted
      'changed'    - an existing $nodecal 0 was flipped to 1
      'unchanged'  - $nodecal 1 already present
      'skipped'    - file could not be understood (left untouched)

    For "patch" materials the key goes into the insert{} block (created if
    absent) because that is what the engine applies on top of the included
    material.
    """
    if not text.strip():
        return text, "skipped"
    bom = ""
    if text.startswith("﻿"):
        bom, text = "﻿", text[1:]
    nl = _detect_newline(text)

    existing = find_nodecal(text)
    if existing:
        s, e, val = existing
        if val.strip().lower() in ("1", "1.0", "true"):
            return bom + text, "unchanged"
        return bom + text[:s] + '"$nodecal" "1"' + text[e:], "changed"

    toks = tokenize(text)
    # top-level: shader name then '{'
    shader = None
    depth = 0
    top_lbrace_end = None
    for n, (kind, val, s, e) in enumerate(toks):
        if kind in ("quoted", "bare") and shader is None and depth == 0:
            shader = val
        elif kind == "lbrace":
            depth += 1
            if depth == 1 and top_lbrace_end is None:
                top_lbrace_end = e
        elif kind == "rbrace":
            depth -= 1
    if shader is None or top_lbrace_end is None:
        return bom + text, "skipped"

    if shader.lower() == "patch":
        # find insert { ... } at depth 1
        depth = 0
        insert_lbrace_end = None
        pending_insert = False
        for kind, val, s, e in toks:
            if kind == "lbrace":
                depth += 1
                if depth == 2 and pending_insert:
                    insert_lbrace_end = e
                    break
                pending_insert = False
            elif kind == "rbrace":
                depth -= 1
                pending_insert = False
            elif kind in ("quoted", "bare") and depth == 1 and val.lower() == "insert":
                pending_insert = True
            elif kind in ("quoted", "bare"):
                pending_insert = False
        if insert_lbrace_end is not None:
            indent = _block_indent(text, insert_lbrace_end)
            ins = f'{nl}{indent}"$nodecal" "1"'
            return bom + text[:insert_lbrace_end] + ins + text[insert_lbrace_end:], "added"
        indent = _block_indent(text, top_lbrace_end)
        ins = f'{nl}{indent}insert{nl}{indent}{{{nl}{indent}\t"$nodecal" "1"{nl}{indent}}}'
        return bom + text[:top_lbrace_end] + ins + text[top_lbrace_end:], "added"

    indent = _block_indent(text, top_lbrace_end)
    ins = f'{nl}{indent}"$nodecal" "1"'
    return bom + text[:top_lbrace_end] + ins + text[top_lbrace_end:], "added"


def decode_text(data: bytes) -> str:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def encode_like(text: str, original: bytes) -> bytes:
    """Re-encode preferring the original encoding (UTF-8 w/ or w/o BOM, else cp1252)."""
    if original.startswith(b"\xef\xbb\xbf"):
        body = text[1:] if text.startswith("﻿") else text
        return b"\xef\xbb\xbf" + body.encode("utf-8")
    try:
        original.decode("utf-8")
        return text.encode("utf-8")
    except UnicodeDecodeError:
        return text.encode("cp1252", errors="replace")


# ------------------------------------------------------------- addonlist.txt
_ADDONLIST_LINE = re.compile(r'^(?P<pre>\s*)"(?P<name>[^"]+)"(?P<mid>\s+)"(?P<val>[01])"(?P<post>\s*)$')


def parse_addonlist(text: str) -> List[Tuple[str, str]]:
    """[(entry, '0'|'1')] in file order.  Entry is e.g. 'workshop\\123.vpk' or 'foo.vpk'."""
    out = []
    for line in text.splitlines():
        m = _ADDONLIST_LINE.match(line)
        if m:
            out.append((m.group("name"), m.group("val")))
    return out


def set_addonlist_values(text: str, changes: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
    """
    Flip the value of existing entries (case-insensitive match on the entry
    name, forward/back slashes treated the same).  Missing entries are
    appended before the closing brace in the file's own style.
    Returns (new_text, previous_values).
    """
    nl = _detect_newline(text)
    lines = text.split(nl) if nl in text else text.splitlines()
    want = {k.replace("/", "\\").lower(): v for k, v in changes.items()}
    prev: Dict[str, str] = {}
    out_lines = []
    for line in lines:
        m = _ADDONLIST_LINE.match(line)
        if m:
            key = m.group("name").replace("/", "\\").lower()
            if key in want:
                prev[m.group("name")] = m.group("val")
                line = f'{m.group("pre")}"{m.group("name")}"{m.group("mid")}"{want.pop(key)}"{m.group("post")}'
        out_lines.append(line)
    if want:
        # append missing entries before the final '}'
        insert_at = None
        for i in range(len(out_lines) - 1, -1, -1):
            if out_lines[i].strip() == "}":
                insert_at = i
                break
        new_lines = [f'\t"{k}"\t\t"{v}"' for k, v in changes.items() if k.replace("/", "\\").lower() in want]
        if insert_at is None:
            out_lines.extend(new_lines)
        else:
            out_lines[insert_at:insert_at] = new_lines
    return nl.join(out_lines), prev
