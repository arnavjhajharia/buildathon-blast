"""Measure what each test costs to run in-process the way runner.py runs it
(process pools inline): CPU ms per test, and per class the setUpClass cost.

usage: cpu_table.py <sqlglot checkout> <tests.json> <out.json>
"""

import concurrent.futures as cf
import importlib
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import runner  # noqa: E402

SRC, TJ, OUT = sys.argv[1:4]
SRC = os.path.abspath(SRC)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, SRC)
os.chdir(SRC)
cf.ProcessPoolExecutor = runner.InlineExecutor
runner.warm()
tests = json.load(open(TJ))["tests"]
byclass = {}
for t in tests:
    f, cls, meth = t["id"].split("::")
    byclass.setdefault((f, cls), []).append(t)
per_test = {}
per_class = {}
skip = {"test_execute_tpcds", "test_max_nodes", "test_generator_memory"}
for (f, cls), ts in byclass.items():
    mod = importlib.import_module(f[:-3].replace("/", "."))
    klass = getattr(mod, cls)
    c0 = time.process_time()
    try:
        klass.setUpClass()
    except Exception as e:  # noqa: BLE001
        print("setup failed", cls, e, file=sys.stderr)
    per_class[f + "::" + cls] = round((time.process_time() - c0) * 1000)
    for t in ts:
        meth = t["id"].rsplit("::", 1)[1]
        if meth in skip:
            per_test[t["number"]] = round(t["seconds"] * 1000)
            continue
        c0 = time.process_time()
        r = unittest.TestResult()
        klass(meth).run(r)
        per_test[t["number"]] = round((time.process_time() - c0) * 1000)
        if r.failures or r.errors:
            print("FAILED in-process:", t["id"], file=sys.stderr)
    try:
        klass.tearDownClass()
        klass.doClassCleanups()
    except Exception:  # noqa: BLE001
        pass
json.dump({"test": per_test, "class": per_class}, open(OUT, "w"))
rec = sum(round(t["seconds"] * 1000) for t in tests if t["id"].rsplit("::", 1)[1] not in skip)
got = sum(v for k, v in per_test.items() if tests[int(k)]["id"].rsplit("::", 1)[1] not in skip)
print(f"recorded {rec} ms, in-process {got} ms, class setups {sum(per_class.values())} ms", file=sys.stderr)
