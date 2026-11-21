"""Blast Radius selector.

For each patch in /data/commits, decide which sqlglot tests can fail and split
them over workers. Logs go to stderr; stdout carries one JSON line per commit.

Evidence used, all built from the pinned sqlglot commit:
  * per-test line coverage (which test executes which source line),
  * which tests open which fixture files,
  * the original source, parsed with `ast`, to see what a changed line belongs to.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SRC = Path(os.environ.get("BLAST_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BLAST_MAP", Path(__file__).resolve().parent / "data" / "covmap.json.gz"))
WORKER_COST_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- diff parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileDiff:
    def __init__(self, old, new):
        self.old = old
        self.new = new
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur = None
    hunk = None
    old = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = None
            hunk = None
        elif line.startswith("--- ") and (hunk is None or hunk_done(hunk)):
            old = line[4:].split("\t")[0]
        elif line.startswith("+++ ") and (hunk is None or hunk_done(hunk)):
            new = line[4:].split("\t")[0]
            cur = FileDiff(strip_prefix(old), strip_prefix(new))
            files.append(cur)
            hunk = None
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            if m:
                os_, ol, ns, nl = m.groups()
                hunk = [int(os_), int(ol if ol is not None else 1), int(ns), int(nl if nl is not None else 1), []]
                cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "+", "-", "\\"):
            if line[:1] != "\\":
                hunk[4].append(line)
        elif hunk is not None and line == "" and not hunk_done(hunk):
            hunk[4].append(" ")
        i += 1
    return files


def hunk_done(h) -> bool:
    old_seen = sum(1 for l in h[4] if l[:1] in (" ", "-"))
    new_seen = sum(1 for l in h[4] if l[:1] in (" ", "+"))
    return old_seen >= h[1] and new_seen >= h[3]


def strip_prefix(p):
    if p is None or p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def apply_hunks(old_text: str, fd: FileDiff) -> str | None:
    src = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into src
    for os_, ol, ns, nl, body in fd.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            return None
        out.extend(src[pos:start])
        pos = start
        for l in body:
            tag, content = l[:1], l[1:]
            if tag == " ":
                if pos >= len(src) or src[pos] != content:
                    return None
                out.append(content)
                pos += 1
            elif tag == "-":
                if pos >= len(src) or src[pos] != content:
                    return None
                pos += 1
            elif tag == "+":
                out.append(content)
    out.extend(src[pos:])
    return "\n".join(out)


def changed_old_lines(fd: FileDiff) -> set[int]:
    """Old-file line numbers touched by the diff. A pure insertion marks the
    lines on both sides of the insertion point."""
    touched = set()
    for os_, ol, ns, nl, body in fd.hunks:
        ln = os_ if ol > 0 else os_ + 1
        prev_kind = None
        for l in body:
            tag = l[:1]
            if tag == " ":
                ln += 1
            elif tag == "-":
                touched.add(ln)
                ln += 1
            elif tag == "+":
                touched.add(ln - 1)
                touched.add(ln)
    return {x for x in touched if x >= 1}


# ---------------------------------------------------------------- AST helpers

def ast_equal(a: str, b: str) -> bool | None:
    try:
        ta = ast.parse(a)
        tb = ast.parse(b)
    except SyntaxError:
        return None
    return ast.dump(ta) == ast.dump(tb)


class Locator:
    """Maps a line of a module to the statement that holds it."""

    def __init__(self, tree: ast.Module):
        self.entries = []  # (start, end, node, scope) scope: 'func' | 'class' | 'module'
        self._walk(tree.body, "module", None)

    def _walk(self, body, scope, owner):
        for node in body:
            end = getattr(node, "end_lineno", node.lineno)
            start = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
            self.entries.append((start, end, node, scope, owner))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk(node.body, "func", node)
            elif isinstance(node, ast.ClassDef):
                self._walk(node.body, "class" if scope != "func" else "func", node)
            else:
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(node, field, None)
                    if sub:
                        for s in sub:
                            if isinstance(s, (ast.ExceptHandler,)) or s.__class__.__name__ == "match_case":
                                self._walk(s.body, scope, owner)
                            else:
                                self._walk([s], scope, owner)

    def innermost(self, line):
        best = None
        for e in self.entries:
            if e[0] <= line <= e[1]:
                if best is None or (e[1] - e[0]) <= (best[1] - best[0]):
                    best = e
        return best


def header_range(node) -> tuple[int, int]:
    """Lines of a statement up to (not including) its nested body."""
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    body = getattr(node, "body", None)
    if isinstance(body, list) and body:
        return start, max(start, body[0].lineno - 1)
    return start, getattr(node, "end_lineno", node.lineno)


# ---------------------------------------------------------------- selection

class Selector:
    def __init__(self):
        tests = json.loads((DATA / "tests.json").read_text())["tests"]
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.all = set(self.ms)
        with gzip.open(MAP, "rt") as fh:
            m = json.load(fh)
        self.sets = m["sets"]
        self.cov = m["files"]
        self.fx = m["fx"]
        self._src_cache = {}

    def covering(self, path, lines) -> set[int]:
        f = self.cov.get(path)
        out = set()
        if not f:
            return out
        lmap = f["l"]
        for ln in lines:
            k = lmap.get(str(ln))
            if k is not None:
                out.update(self.sets[k])
        return out

    def file_tests(self, path) -> set[int]:
        f = self.cov.get(path)
        out = set()
        if f:
            for k in set(f["l"].values()):
                out.update(self.sets[k])
        return out

    def source(self, path):
        if path not in self._src_cache:
            p = SRC / path
            self._src_cache[path] = p.read_text(encoding="utf-8") if p.is_file() else None
        return self._src_cache[path]

    def select(self, patch_text: str) -> set[int]:
        sel = set()
        for fd in parse_patch(patch_text):
            sel |= self.select_file(fd)
        return sel

    def select_file(self, fd: FileDiff) -> set[int]:
        path = fd.old or fd.new
        if fd.old is None or fd.new is None or fd.old != fd.new:
            log("  add/delete/rename", fd.old, fd.new, "-> all")
            return set(self.all)
        old = self.source(path)
        if old is None:
            log("  unknown file", path, "-> all")
            return set(self.all)
        new = apply_hunks(old, fd)
        if new is None:
            log("  patch does not apply", path, "-> all")
            return set(self.all)
        if path.endswith(".py") and path.startswith("sqlglot/"):
            return self.select_py(path, old, new, fd)
        if path in self.fx:
            return set(self.fx[path])
        if path.startswith("tests/") and path.endswith(".py"):
            return self.select_testfile(path, old, new, fd)
        if path.startswith("tests/fixtures/"):
            log("  fixture read by no test", path)
            return set()
        log("  other file", path, "-> all")
        return set(self.all)

    def select_testfile(self, path, old, new, fd):
        if path in self.by_file:
            eq = ast_equal(old, new)
            if eq:
                return set()
            return set(self.by_file[path])
        return set(self.all)

    def select_py(self, path, old, new, fd) -> set[int]:
        eq = ast_equal(old, new)
        if eq:
            return set()
        if eq is None:
            log("  syntax error in", path, "-> tests touching file + all")
            return set(self.all)
        tree = ast.parse(old)
        loc = Locator(tree)
        out = set()
        for ln in sorted(changed_old_lines(fd)):
            e = loc.innermost(ln)
            if e is None:
                continue  # blank/comment line outside any statement
            start, end, node, scope, owner = e
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hs, he = header_range(node)
                if hs <= ln <= he:
                    # signature/decorator change: everything that runs the function
                    out |= self.covering(path, range(node.lineno, node.end_lineno + 1))
                    if scope != "func":
                        out |= self.import_level(path, node, owner, loc)
                    continue
            if isinstance(node, ast.ClassDef):
                hs, he = header_range(node)
                if hs <= ln <= he:
                    out |= self.import_level(path, node, owner, loc)
                    continue
            if scope == "func":
                hs, he = header_range(node)
                if hs <= ln <= he:
                    out |= self.covering(path, range(hs, he + 1))
                else:
                    out |= self.covering(path, range(start, end + 1))
            else:
                out |= self.import_level(path, node, owner, loc)
        return out

    def import_level(self, path, node, owner, loc) -> set[int]:
        """A statement that runs at import time (module or class body)."""
        # v1: everything that runs any code of this file, plus runtime coverage of
        # the statement's own lines (lambdas inside it).
        return self.file_tests(path) | self.covering(path, range(node.lineno, node.end_lineno + 1))


# ---------------------------------------------------------------- packing

def pack(tests: set[int], ms: dict[int, int]) -> list[list[int]]:
    if not tests:
        return []
    order = sorted(tests, key=lambda t: (-ms[t], t))
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            loads[i] += ms[t]
            bins[i].append(t)
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return [b for b in best[1] if b]


def main() -> None:
    sel = Selector()
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            tests = sel.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never crash: run everything
            log(f"{cid}: error {e!r} -> all")
            tests = set(sel.all)
        workers = pack(tests, sel.ms)
        log(f"{cid}: {len(tests)} tests, {len(workers)} workers")
        print(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
