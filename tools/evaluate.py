"""Score a selector output against answers, per commit. usage: evaluate.py <setdir> <output.jsonl>"""
import json, sys
S, OUT = sys.argv[1], sys.argv[2]
T = json.load(open(S + '/tests.json'))['tests']
ms = {t['number']: round(t['seconds'] * 1000) for t in T}
A = json.load(open(S + '/answers.json'))['commits']
got = {}
for line in open(OUT):
    if line.strip():
        o = json.loads(line); got[o['commit']] = o['workers']
tot = 0; miss = 0; rows = []
for c in sorted(A):
    w = got.get(c, [])
    w = [x for x in w if x]
    cost = (max(sum(ms[t] for t in x) for x in w) + 2000 * len(w)) if w else 0
    sel = {t for x in w for t in x}
    missed = [t for t in A[c]['failing'] if t not in sel]
    m = 1 if missed else 0
    tot += cost + 300000 * m; miss += m
    rows.append((c, A[c].get('kind', '?'), len(A[c]['failing']), len(sel), len(w), cost / 1000, 'MISS ' + ','.join(map(str, missed[:5])) if m else ''))
if '-v' in sys.argv:
    for r in rows: print(*r)
print(f'n={len(A)} simulated={tot/1000:.1f}s misses={miss} nonmiss_cost={(tot-300000*miss)/1000:.1f}')
