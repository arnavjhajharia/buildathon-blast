# pytest plugin: write the node ids of every test that fails (call, setup, teardown,
# any failed subtest) or whose file fails to import. Output: $FAILOUT/<pid>.txt
import os

import pytest

_failed = set()
_errors = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _errors.add(report.nodeid)


def pytest_collection_modifyitems(session, config, items):
    only = os.environ.get("TRUTH_ONLY")
    if not only:
        return
    keep = set(open(only).read().split("\n"))
    sel = [i for i in items if i.nodeid in keep]
    desel = [i for i in items if i.nodeid not in keep]
    if desel:
        config.hook.pytest_deselected(items=desel)
    items[:] = sel


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("FAILOUT")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"{os.getpid()}.txt"), "w") as fh:
        for n in sorted(_failed):
            fh.write("F " + n + "\n")
        for n in sorted(_errors):
            fh.write("E " + n + "\n")
