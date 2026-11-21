# pytest plugin: record which repo files each test opens (fixture map). phase-aware.
import builtins, io, os, json, atexit
import pytest
_cur = [None]
_rec = {}
_root = os.path.abspath(os.getcwd())
_orig_open = builtins.open
_orig_ioopen = io.open
def _note(f):
    try:
        p = os.path.abspath(os.fspath(f))
    except Exception:
        return
    if p.startswith(_root + '/') and '__pycache__' not in p:
        k = _cur[0] or '<collect>'
        _rec.setdefault(k, set()).add(os.path.relpath(p, _root))
def _open(f, *a, **kw):
    _note(f); return _orig_open(f, *a, **kw)
def _ioopen(f, *a, **kw):
    _note(f); return _orig_ioopen(f, *a, **kw)
builtins.open = _open
io.open = _ioopen
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    _cur[0] = item.nodeid + '|setup'
    yield
    _cur[0] = item.nodeid + '|run'
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    _cur[0] = item.nodeid + '|run'
    yield
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    _cur[0] = item.nodeid + '|teardown'
    yield
    _cur[0] = None
def _dump():
    out = os.environ.get('FXOUT', '/tmp/fx')
    os.makedirs(out, exist_ok=True)
    with _orig_open(os.path.join(out, f'fx_{os.getpid()}.json'), 'w') as fh:
        json.dump({k: sorted(v) for k, v in _rec.items()}, fh)
atexit.register(_dump)
