"""Run chosen sqlglot tests against a patched sqlglot, inside this container.

A fork server: the parent imports the heavy third-party modules once; for each
commit a child is forked, installs an import hook that serves the patched
source of the changed modules, imports the test modules (and so a fresh
sqlglot), runs the tests with unittest and reports the ids that failed.
Process pools in the tests run inline (with the same pickling round trip),
so no interpreter is spawned and the patched code is what runs.
"""

from __future__ import annotations

import concurrent.futures as cf
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pickle
import signal
import sys
import time
import unittest


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, source):
        super().__init__(name, path)
        self._source = source

    def get_code(self, fullname):
        return compile(self._source, self.path, "exec", dont_inherit=True)

    def get_source(self, fullname):
        return self._source


class _PatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self, by_path: dict[str, str]):
        self.by_path = by_path

    def find_spec(self, name, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.origin not in self.by_path:
            return None
        loader = _PatchedLoader(name, spec.origin, self.by_path[spec.origin])
        new = importlib.util.spec_from_file_location(
            name, spec.origin, loader=loader,
            submodule_search_locations=spec.submodule_search_locations,
        )
        return new


class InlineExecutor(cf.Executor):
    """Stands in for ProcessPoolExecutor: runs each call at once, in process,
    pickling arguments and results the way a pool would."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn2(*a2, **k2)
            f.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


class _Result(unittest.TestResult):
    """Collects failing tests; stops at the first one when asked (one is enough
    to know the change is live)."""

    def __init__(self, stop_first=True):
        super().__init__()
        self.bad = set()
        self.stop_first = stop_first

    def _bad(self, test):
        self.bad.add(_tid(test))
        if self.stop_first:
            self.stop()

    def addError(self, test, err):
        self._bad(test)

    def addFailure(self, test, err):
        self._bad(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._bad(test)

    def addUnexpectedSuccess(self, test):
        self._bad(test)

    def addSkip(self, test, reason):
        # a test skipped here may run (and fail) on the scorer: no answer
        self._bad(test)


def _tid(test):
    # class-level errors (setUpClass) arrive as _ErrorHolder
    if isinstance(test, unittest.case._SubTest):
        test = test.test_case
    if hasattr(test, "_testMethodName") and test.__class__.__name__ != "_ErrorHolder":
        return f"{test.__class__.__module__}.{test.__class__.__name__}.{test._testMethodName}"
    return "CLASS:" + str(getattr(test, "description", test))


def _child(root: str, patched: dict[str, str], ids: list[str], wfd: int) -> None:
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, root)
    cf.ProcessPoolExecutor = InlineExecutor
    sys.meta_path.insert(0, _PatchFinder({os.path.join(root, p): s for p, s in patched.items()}))
    bad = set()
    import_failed = set()
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    modules = {}
    for tid in ids:
        f, cls, meth = tid.split("::")
        mod = f[:-3].replace("/", ".")
        if mod not in modules:
            try:
                modules[mod] = importlib.import_module(mod)
            except BaseException:  # noqa: BLE001
                modules[mod] = None
        if modules[mod] is None:
            import_failed.add(tid)
            continue
        try:
            suite.addTest(getattr(modules[mod], cls)(meth))
        except BaseException:  # noqa: BLE001
            import_failed.add(tid)
    res = _Result()
    if not import_failed:
        try:
            suite.run(res)
        except BaseException:  # noqa: BLE001
            res.bad.add("CLASS:crash")
    byname = {}
    for tid in ids:
        f, cls, meth = tid.split("::")
        byname[f"{f[:-3].replace('/', '.')}.{cls}.{meth}"] = tid
    for b in res.bad:
        if b in byname:
            bad.add(byname[b])
        else:
            bad.add(b)  # class-level failure: caller treats as unknown
    bad |= import_failed
    os.write(wfd, json.dumps(sorted(bad)).encode())
    os.close(wfd)


def run_tests(root: str, patched: dict[str, str], ids: list[str], timeout: float) -> list[str] | None:
    """Return failing ids, or None when the run could not finish (timeout, crash)."""
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            # keep stdout clean: tests may print
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            sys.stdout = open(os.devnull, "w")
            sys.stderr = open(os.devnull, "w")
            _child(root, patched, ids, w)
        except BaseException:  # noqa: BLE001
            pass
        os._exit(0)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + timeout
    import select

    done = False
    while not done:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        rl, _, _ = select.select([r], [], [], left)
        if rl:
            b = os.read(r, 65536)
            if not b:
                done = True
            else:
                chunks.append(b)
    if not done:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    os.close(r)
    os.waitpid(pid, 0)
    if not done or not chunks:
        return None
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return None


def warm():
    """Imports shared by every child, done once in the parent."""
    import unittest.mock  # noqa: F401
    import pickle  # noqa: F401
    for m in ("numpy", "pandas", "pandas.testing", "duckdb"):
        try:
            importlib.import_module(m)
        except Exception:  # noqa: BLE001
            pass
