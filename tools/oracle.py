import json,sys
S=sys.argv[1]
T=json.load(open(S+'/tests.json'))['tests']
A=json.load(open(S+'/answers.json'))['commits']
ms=[round(t['seconds']*1000) for t in T]
def best(ids):
    if not ids: return 0
    d=sorted((ms[i] for i in ids),reverse=True); bestc=None
    for k in range(1,9):
        L=[0]*k
        for x in d: L[L.index(min(L))]+=x
        c=max(L)+2000*k
        bestc=c if bestc is None else min(bestc,c)
    return bestc
tot=0
for c,v in sorted(A.items()):
    b=best(v['failing']); tot+=b; print(c,v['kind'],b/1000)
print('oracle total',tot/1000)
