# pytest plugin: attribute lines executed inside ProcessPoolExecutor children to the submitting test.
import os, sys, json
import pytest
import concurrent.futures as cf

_cur = [None]
OUT = os.environ.get('SUBCOV_OUT', '/tmp/subcov')
TOOL = 3
_lines = {}
_started = [False]

def _line(code, ln):
    f = code.co_filename
    i = f.find('/sqlglot/')
    if i >= 0 and '/tests/' not in f:
        _lines.setdefault(f[i + 1:], set()).add(ln)
    return sys.monitoring.DISABLE

def _start():
    m = sys.monitoring
    if not _started[0]:
        m.use_tool_id(TOOL, 'subcov')
        m.register_callback(TOOL, m.events.LINE, _line)
        _started[0] = True
    _lines.clear()
    m.restart_events()
    m.set_events(TOOL, m.events.LINE)

def _stop(nodeid):
    m = sys.monitoring
    m.set_events(TOOL, 0)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f'{os.getpid()}.jsonl'), 'a') as fh:
        fh.write(json.dumps({'t': nodeid, 'l': {k: sorted(v) for k, v in _lines.items()}}) + '\n')

def _ctx_call(nodeid, payload):
    # unpickle the call and pickle its result while tracing, so serde code used by
    # pickling is attributed to the test too
    import pickle
    _start()
    try:
        fn, a, kw = pickle.loads(payload)
        res = fn(*a, **kw)
        pickle.dumps(res)
        return res
    finally:
        _stop(nodeid)

def _ctx_init(nodeid, fn, *a):
    _start()
    try:
        if fn is not None:
            return fn(*a)
    finally:
        _stop(nodeid)

_orig_submit = cf.ProcessPoolExecutor.submit
_orig_init = cf.ProcessPoolExecutor.__init__

def _submit(self, fn, /, *a, **kw):
    import pickle
    return _orig_submit(self, _ctx_call, _cur[0] or '<none>', pickle.dumps((fn, a, kw)))

def _init(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
    if initializer is not None:
        initargs = (_cur[0] or '<none>', initializer) + tuple(initargs)
        initializer = _ctx_init
    return _orig_init(self, max_workers, mp_context, initializer, initargs, **kw)

cf.ProcessPoolExecutor.submit = _submit
cf.ProcessPoolExecutor.__init__ = _init

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    _cur[0] = item.nodeid
    yield
    _cur[0] = None
