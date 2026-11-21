"""What-if analysis of giant-skip dynamic policy. usage: whatif.py <setdir> [giant_ms]"""
import json, sys, os, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
S = sys.argv[1]; G = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
os.environ['BLAST_DATA'] = S
spec = importlib.util.spec_from_file_location("sel", os.path.join(HERE, "..", "selector.py"))
sel = importlib.util.module_from_spec(spec); spec.loader.exec_module(sel)
A = json.load(open(S + '/answers.json'))['commits']
X = sel.Selector()
ms = X.ms
def cost(w): return (max(sum(ms[t] for t in b) for b in w) + 2000 * len(w)) if w else 0
tot_s = tot_d = 0
for c in sorted(A):
    s = X.select(open(f'{S}/commits/{c}.patch').read())
    fail = set(A[c]['failing'])
    giants = {t for t in s if ms[t] >= G}
    R = s - giants
    cs = cost(sel.pack(s, ms)) + (300000 if fail - s else 0)
    if giants:
        F = fail & R
        gf = fail & giants
        # run R cheapest first, stop at first failure
        run = 0
        for t in sorted(R, key=lambda t: ms[t]):
            run += ms[t]
            if t in F: break
        if F:
            cd = run + cost(sel.pack(s, ms))
        else:
            cd = run + (300000 if gf else 0)
        note = f'giants={len(giants)} R={len(R)} sumR={sum(ms[t] for t in R)/1000:.1f} F={len(F)} giantfail={len(gf)}'
    else:
        cd = cs; note = ''
    tot_s += cs; tot_d += cd
    print(c, A[c].get('kind'), f'static={cs/1000:.1f} dyn={cd/1000:.1f}', note)
print(f'static={tot_s/1000:.1f} dyn={tot_d/1000:.1f} (dyn excludes per-run overhead)')
