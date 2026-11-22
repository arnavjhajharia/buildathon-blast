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
    return set(changed_spans(fd))


def changed_spans(fd: FileDiff) -> dict[int, tuple[int, int] | None]:
    """For each changed old line, the changed character span (start, end) when a
    block of removed lines is replaced by the same number of added lines, else None
    (the whole line)."""
    spans: dict[int, tuple[int, int] | None] = {}
    for os_, ol, ns, nl, body in fd.hunks:
        ln = os_ if ol > 0 else os_ + 1
        i = 0
        while i < len(body):
            if body[i][:1] == " ":
                ln += 1
                i += 1
                continue
            minus, plus = [], []
            while i < len(body) and body[i][:1] == "-":
                minus.append(body[i][1:])
                i += 1
            while i < len(body) and body[i][:1] == "+":
                plus.append(body[i][1:])
                i += 1
            if minus and len(minus) == len(plus):
                for k, (a, b) in enumerate(zip(minus, plus)):
                    p = 0
                    while p < min(len(a), len(b)) and a[p] == b[p]:
                        p += 1
                    q = 0
                    while q < min(len(a), len(b)) - p and a[len(a) - 1 - q] == b[len(b) - 1 - q]:
                        q += 1
                    spans[ln + k] = (p, max(p, len(a) - q))
            else:
                for k in range(len(minus)):
                    spans[ln + k] = None
                if not minus:
                    spans.setdefault(ln - 1, None)
                    spans.setdefault(ln, None)
            ln += len(minus)
    return {k: v for k, v in spans.items() if k >= 1}


def _pos_le(a, b):
    return a[0] < b[0] or (a[0] == b[0] and a[1] <= b[1])


def node_path(stmt, line: int, ca: int, cb: int, text_line: str):
    """AST nodes from stmt down to the innermost node containing the span
    (line, ca..cb) given in characters of the old line."""
    bca = len(text_line[:ca].encode("utf-8"))
    bcb = len(text_line[:cb].encode("utf-8"))
    lo, hi = (line, bca), (line, bcb)
    path = [stmt]
    node = stmt
    while True:
        nxt = None
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or getattr(child, "end_lineno", None) is None:
                # arguments/keywords without positions: descend if any of their parts match
                if isinstance(child, (ast.arguments,)):
                    for sub in ast.iter_child_nodes(child):
                        if hasattr(sub, "lineno") and _pos_le((sub.lineno, sub.col_offset), lo) and _pos_le(hi, (sub.end_lineno, sub.end_col_offset)):
                            path.append(child)
                            nxt = sub
                            break
                if nxt is not None:
                    break
                continue
            if _pos_le((child.lineno, child.col_offset), lo) and _pos_le(hi, (child.end_lineno, child.end_col_offset)):
                nxt = child
                break
        if nxt is None:
            return path
        path.append(nxt)
        node = nxt


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
        self.ids = {t["number"]: t["id"] for t in tests}
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
        self.evidence = []  # (kind, covering tests) per changed statement of the current commit

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
        spans = changed_spans(fd)
        old_lines = old.split("\n")
        for ln in sorted(changed_old_lines(fd)):
            e = loc.innermost(ln)
            if e is None:
                continue  # blank/comment line outside any statement
            start, end, node, scope, owner = e
            span = spans.get(ln)
            if span is not None and ln - 1 < len(old_lines):
                got = self.inner_scope(path, node, ln, span, old_lines[ln - 1])
                if got is not None:
                    self.evidence.append(("func", got))
                    out |= got
                    continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hs, he = header_range(node)
                if hs <= ln <= he:
                    # signature/decorator change: everything that runs the function
                    got = self.covering(path, range(node.lineno, node.end_lineno + 1))
                    if scope != "func":
                        got |= self.import_level(path, node, owner, loc)
                    self.evidence.append(("import", got))
                    out |= got
                    continue
            if isinstance(node, ast.ClassDef):
                hs, he = header_range(node)
                if hs <= ln <= he:
                    got = self.import_level(path, node, owner, loc)
                    self.evidence.append(("import", got))
                    out |= got
                    continue
            if scope == "func":
                hs, he = header_range(node)
                if hs <= ln <= he:
                    got = self.covering(path, range(hs, he + 1))
                else:
                    got = self.covering(path, range(start, end + 1))
                self.evidence.append(("func", got))
            else:
                got = self.import_level(path, node, owner, loc)
                self.evidence.append(("import", got))
            out |= got
        return out

    def inner_scope(self, path, stmt, ln, span, text_line):
        """When the changed characters sit inside code that only runs when called
        (a lambda body, or a default value of a parameter), return the tests that
        ran that code; else None."""
        nodes = node_path(stmt, ln, span[0], span[1], text_line)
        for i in range(len(nodes) - 1, -1, -1):
            n = nodes[i]
            if isinstance(n, ast.Lambda) and i + 1 < len(nodes) and nodes[i + 1] is n.body:
                return self.covering(path, range(n.body.lineno, n.body.end_lineno + 1))
            if isinstance(n, ast.arguments) and i > 0 and isinstance(nodes[i - 1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                f = nodes[i - 1]
                # a default value: only calls that rely on it see the change
                if i + 1 < len(nodes) and any(nodes[i + 1] is d for d in list(n.defaults) + [d for d in n.kw_defaults if d is not None]):
                    body = f.body if isinstance(f, ast.Lambda) else f.body
                    if isinstance(body, list):
                        return self.covering(path, range(body[0].lineno, f.end_lineno + 1))
                    return self.covering(path, range(body.lineno, body.end_lineno + 1))
                return None
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and i > 0:
                return None
        return None

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


def cost_of(workers, ms) -> int:
    return (max(sum(ms[t] for t in w) for w in workers) + WORKER_COST_MS * len(workers)) if workers else 0


# ---------------------------------------------------------------- dynamic check
#
# Running a test here costs its CPU time; listing it costs its time on the
# busiest worker plus 2 s a worker. Running pays off when the listed set is
# dominated by a few long tests (GIANT_MS and up): if every shorter test that
# reaches the change passes here, the change did not alter behaviour those tests
# can see, and the long tests are left out.

DYNAMIC = os.environ.get("BLAST_DYNAMIC", "1") == "1"
GIANT_MS = int(os.environ.get("BLAST_GIANT_MS", "3000"))
P_LIVE = float(os.environ.get("BLAST_P_LIVE", "0.5"))
OVERHEAD_MS = int(os.environ.get("BLAST_OVERHEAD_MS", "150"))
CPU_SCALE = float(os.environ.get("BLAST_CPU_SCALE", "1.5"))  # server CPU per measured ms
MIN_WITNESSES = int(os.environ.get("BLAST_MIN_WITNESSES", "3"))
RUN_TIMEOUT = float(os.environ.get("BLAST_RUN_TIMEOUT", "60"))
HEAVY_IMPORTS = {"tests/test_executor.py", "tests/test_optimizer.py"}
# tests that start interpreters or measure memory of the process they run in: never
# judged here, always listed when selected
UNVERIFIABLE = {
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    "tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries",
}


class Plan:
    def __init__(self, cid, static, patched, verifiable, evidence=()):
        self.evidence = list(evidence)
        self.cid = cid
        self.static = static
        self.patched = patched
        self.verifiable = verifiable
        self.final = static
        self.order = None
        self.keep = None


def plan_commit(sel: Selector, cid: str, text: str) -> Plan:
    fds = parse_patch(text)
    static = set()
    patched = {}
    verifiable = True
    sel.evidence = []
    for fd in fds:
        static |= sel.select_file(fd)
        path = fd.old
        if fd.old is None or fd.new is None or fd.old != fd.new:
            verifiable = False
            continue
        old = sel.source(path)
        if old is None:
            verifiable = False
            continue
        if path.startswith("sqlglot/") and path.endswith(".py"):
            new = apply_hunks(old, fd)
            eq = None if new is None else ast_equal(old, new)
            if eq is None:
                verifiable = False
            elif not eq:
                patched[path] = new
        else:
            # fixtures and test files: these changes nearly always fail their readers
            verifiable = False
    return Plan(cid, static, patched, verifiable, sel.evidence)


class Costs:
    """CPU this container spends running tests in process (measured at the pinned
    commit, scaled), used to decide when running beats listing."""

    def __init__(self, sel: Selector):
        self.sel = sel
        p = MAP.parent / "cputable.json"
        t = json.loads(p.read_text()) if p.is_file() else {"test": {}, "class": {}}
        self.test = {int(k): v for k, v in t["test"].items()}
        self.klass = t["class"]

    def run_ms(self, t):
        return CPU_SCALE * self.test.get(t, self.sel.ms[t])

    def order(self, tests):
        ids = self.sel.ids
        groups = {}
        for t in tests:
            groups.setdefault(ids[t].rsplit("::", 1)[0], []).append(t)
        for g in groups.values():
            g.sort(key=lambda t: (self.run_ms(t), t))
        keys = sorted(groups, key=lambda k: (self.klass.get(k, 0) + self.run_ms(groups[k][0]), k))
        return [t for k in keys for t in groups[k]]

    def cost_until(self, order, stop_at=None):
        """CPU ms to run `order`, stopping after the first test in stop_at."""
        ids = self.sel.ids
        seen = set()
        total = OVERHEAD_MS
        for t in order:
            k = ids[t].rsplit("::", 1)[0]
            if k not in seen:
                seen.add(k)
                total += CPU_SCALE * self.klass.get(k, 0)
            total += self.run_ms(t)
            if stop_at and t in stop_at:
                break
        return total


def dynamic_choice(sel: Selector, costs: Costs, p: Plan) -> bool:
    """Fill p.order (tests to run here, in order) and p.keep (tests listed when all
    of them pass); return False to stay static."""
    if not DYNAMIC or not p.verifiable or not p.static or not p.patched:
        return False
    ms = sel.ms
    unver = {t for t in p.static if sel.ids[t] in UNVERIFIABLE}
    giants = {t for t in p.static if ms[t] >= GIANT_MS} - unver
    run = p.static - giants - unver
    if not run:
        return False
    # giants are dropped when every shorter test passes, but only if every changed
    # statement is reached by at least MIN_WITNESSES shorter tests (a statement only a
    # long test reaches, like a size limit, is judged by that test alone)
    skip_ok = all(len(got - giants - unver) >= MIN_WITNESSES for kind, got in p.evidence)
    static_cost = cost_of(pack(p.static, ms), ms)
    # tests that cost far more to run here than to list (process pools run inline)
    dear = {t for t in run if costs.run_ms(t) > 2 * ms[t] and costs.run_ms(t) > 300}
    best = None
    for listed in ({frozenset()} | ({frozenset(dear)} if dear else set())):
        r = run - listed
        if not r:
            continue
        keep = unver | listed | (set() if skip_ok else giants)
        order = costs.order(r)
        run_ms = costs.cost_until(order)
        pass_cost = cost_of(pack(keep, ms), ms)
        # a live change is usually caught early: charge part of the run
        expected = P_LIVE * (0.3 * run_ms + static_cost) + (1 - P_LIVE) * (run_ms + pass_cost)
        if best is None or expected < best[0]:
            best = (expected, order, keep)
    if best is None or best[0] >= static_cost:
        return False
    p.order = best[1]
    p.keep = best[2]
    return True


def _cpu() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def decide(sel: Selector, plans, run_fn) -> None:
    """run_fn(plan) -> list of failing ids, or None when the run gave no answer."""
    costs = Costs(sel)
    for p in plans:
        try:
            if not dynamic_choice(sel, costs, p):
                continue
            bad = run_fn(p)
        except Exception as e:  # noqa: BLE001
            log(f"{p.cid}: dynamic stage error {e!r}; static kept")
            continue
        if bad is None or bad:
            log(f"{p.cid}: ran {len(p.order)} here (cpu so far {_cpu():.2f}s): {'no answer' if bad is None else len(bad)} failing -> static")
            p.final = p.static
        else:
            log(f"{p.cid}: ran {len(p.order)} here (cpu so far {_cpu():.2f}s), all pass -> list {len(p.keep)}")
            p.final = p.keep


def main() -> None:
    sel = Selector()
    plans = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            p = plan_commit(sel, cid, patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never crash: run everything
            log(f"{cid}: error {e!r} -> all")
            p = Plan(cid, set(sel.all), {}, False)
        plans.append(p)

    state = {"warm": False}

    def run_here(p):
        import runner

        files = {sel.ids[t].split("::", 1)[0] for t in p.order}
        if not state["warm"] and files & HEAVY_IMPORTS:
            runner.warm()
            state["warm"] = True
        return runner.run_tests(str(SRC), p.patched, [sel.ids[t] for t in p.order], RUN_TIMEOUT)

    decide(sel, plans, run_here)

    for p in plans:
        workers = pack(p.final, sel.ms)
        log(f"{p.cid}: {len(p.final)} tests, {len(workers)} workers")
        print(json.dumps({"commit": p.cid, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
