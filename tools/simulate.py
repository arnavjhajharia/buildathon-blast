"""Simulate the selector's full policy on a labelled set without running tests:
the in-container runs are answered from the labels (assumes the runner agrees
with pytest, which runner_check.py measures separately) and their CPU is
estimated from data/cputable.json.

usage: simulate.py <set dir> [-v]       (BLAST_* env vars tune the policy)
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
S = sys.argv[1]
os.environ["BLAST_DATA"] = S
spec = importlib.util.spec_from_file_location("sel", os.path.join(HERE, "..", "selector.py"))
sel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sel)
sel.log = lambda *a: None

A = json.load(open(os.path.join(S, "answers.json")))["commits"]
X = sel.Selector()
costs = sel.Costs(X)
plans = []
for c in sorted(A):
    text = open(os.path.join(S, "commits", c + ".patch")).read()
    plans.append(sel.plan_commit(X, c, text))

cpu = {}


def fake_run(p, timeout=None):
    fail = set(A[p.cid]["failing"])
    cpu[p.cid] = costs.cost_until(p.order, stop_at=fail)
    hit = [X.ids[t] for t in p.order if t in fail]
    return hit[:1]


sel.decide(X, plans, fake_run)
tot = miss = 0
tot_static = miss_static = 0
cpu_total = sum(cpu.values())
rows = []
for p in plans:
    fail = set(A[p.cid]["failing"])
    w = sel.pack(p.final, X.ms)
    c = sel.cost_of(w, X.ms)
    m = bool(fail - p.final)
    ws = sel.pack(p.static, X.ms)
    cs = sel.cost_of(ws, X.ms)
    ms_ = bool(fail - p.static)
    tot += c + 300000 * m
    miss += m
    tot_static += cs + 300000 * ms_
    miss_static += ms_
    rows.append((p.cid, A[p.cid].get("kind"), len(fail), len(p.static), cs / 1000, len(p.final), c / 1000,
                 round(cpu.get(p.cid, 0) / 1000, 2), "MISS" if m else ""))
if "-v" in sys.argv:
    for r in rows:
        print(*r)
n = len(plans)
print(f"n={n} static: {tot_static/1000:.1f}s misses={miss_static} | policy: simulated={tot/1000:.1f}s misses={miss} "
      f"est_run_cpu={cpu_total/1000:.1f}s total={(tot+cpu_total)/1000:.1f}s dynamic_commits={len(cpu)}")
