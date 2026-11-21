"""Label a set of patches by running sqlglot's real tests on each one.

usage: truth.py <scratch sqlglot checkout> <set dir> <tests.json> [xdist workers]
Applies each <set>/commits/<id>.patch to the scratch checkout, runs the whole
suite (SKIP_INTEGRATION=0), records failing tests, restores the files.
Writes <set>/answers.json (same shape as the sample) and copies tests.json.
Resumable: commits already in answers.json are skipped.
"""

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sel", os.path.join(HERE, "..", "selector.py"))
sel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sel)

SCRATCH, SET, TJ = sys.argv[1], sys.argv[2], sys.argv[3]
PRISTINE = os.environ.get("TRUTH_PRISTINE", SCRATCH)
N = sys.argv[4] if len(sys.argv) > 4 else "4"
tests = json.load(open(TJ))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
shutil.copy(TJ, os.path.join(SET, "tests.json"))
ans_path = os.path.join(SET, "answers.json")
answers = json.load(open(ans_path))["commits"] if os.path.exists(ans_path) else {}
kinds = {}
if os.path.exists(os.path.join(SET, "kinds.json")):
    kinds = json.load(open(os.path.join(SET, "kinds.json")))


X = sel.Selector() if os.environ.get("TRUTH_FULL") != "1" else None
idof = {t["number"]: t["id"] for t in tests}


def superset(fds):
    """Tests that can possibly be affected, generously: every test that ran any
    line of a changed module (function-body edits only), or that opens a changed
    fixture. None = run everything (import-level edits, anything unusual)."""
    if X is None:
        return None
    import ast as _ast
    out = set()
    for fd in fds:
        path = fd.old
        if fd.old != fd.new or path is None:
            return None
        if path.startswith("sqlglot/") and path.endswith(".py"):
            old = X.source(path)
            loc = sel.Locator(_ast.parse(old))
            for ln in sel.changed_old_lines(fd):
                e = loc.innermost(ln)
                if e is None:
                    continue
                start, end, node, scope, owner = e
                if scope != "func" or isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                    return None
            out |= X.file_tests(path)
        elif path.startswith("tests/fixtures/"):
            readers = set(X.fx.get(path, []))
            files = {idof[n].split("::")[0] for n in readers}
            if not files:
                return None
            out |= {n for f in files for n in by_file[f]}
        else:
            return None
    return out


def drop_pyc(path):
    d = os.path.join(os.path.dirname(path), "__pycache__")
    stem = os.path.splitext(os.path.basename(path))[0]
    for f in glob.glob(os.path.join(d, stem + ".*.pyc")):
        os.remove(f)


for patch in sorted(glob.glob(os.path.join(SET, "commits", "*.patch"))):
    cid = os.path.basename(patch)[:-6]
    if cid in answers:
        continue
    fds = sel.parse_patch(open(patch).read())
    saved = {}
    ok = True
    for fd in fds:
        # originals always come from the pristine checkout, never from scratch
        old = open(os.path.join(PRISTINE, fd.old), encoding="utf-8").read()
        new = sel.apply_hunks(old, fd)
        if new is None:
            ok = False
            break
        saved[os.path.join(SCRATCH, fd.old)] = (old, new)
    if not ok:
        print(cid, "does not apply", file=sys.stderr)
        continue
    # wait for disk space on the system volume (shared machine)
    while shutil.disk_usage("/").free < 150 * 2**20:
        print("low disk, waiting", file=sys.stderr, flush=True)
        time.sleep(30)
    out = tempfile.mkdtemp()
    env = dict(os.environ, SKIP_INTEGRATION="0", FAILOUT=out, PYTHONPATH=".:" + HERE)
    scope = superset(fds)
    if scope is not None:
        with open(os.path.join(out, "only.lst"), "w") as fh:
            fh.write("\n".join(idof[n] for n in sorted(scope)))
        env["TRUTH_ONLY"] = os.path.join(out, "only.lst")
    t0 = time.time()
    try:
        for p, (old, new) in saved.items():
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new)
            drop_pyc(p)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-n", N, "-p", "no:cacheprovider",
             "-p", "failplugin", "--timeout", "300", "tests"],
            cwd=SCRATCH, env=env, capture_output=True, text=True,
        )
    finally:
        for p, (old, new) in saved.items():
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(old)
            drop_pyc(p)
    failing = set()
    errors = []
    for f in glob.glob(os.path.join(out, "*.txt")):
        for line in open(f):
            k, nid = line.rstrip("\n").split(" ", 1)
            if k == "F":
                if nid in num:
                    failing.add(num[nid])
                else:
                    errors.append(nid)
            else:
                # a file that cannot be imported fails all its tests
                hit = [n for f2, ns in by_file.items() if f2 == nid or f2.startswith(nid.rstrip("/") + "/") for n in ns]
                failing.update(hit)
                errors.append("collect:" + nid)
    shutil.rmtree(out, ignore_errors=True)
    if "error" in r.stdout.splitlines()[-1:][0] if r.stdout else False:
        pass
    if shutil.disk_usage("/").free < 60 * 2**20:
        print(cid, "disk ran low during the run; not recorded", file=sys.stderr, flush=True)
        continue
    answers[cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?")}
    print(f"{cid} {kinds.get(cid,'?')} scope={'all' if scope is None else len(scope)} failing={len(failing)} {time.time()-t0:.0f}s {errors[:3]} | {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''}", flush=True)
    with open(ans_path, "w") as fh:
        json.dump({"commits": answers}, fh)
