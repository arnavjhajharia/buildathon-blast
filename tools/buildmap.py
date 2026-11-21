"""Build compact per-test line coverage map from a coverage.py db with pytest-cov test contexts.
usage: buildmap.py <.coverage> <tests.json> <srcroot> <out.json.gz> [fxdir]"""
import sys, json, gzip, os, glob, collections
from coverage import CoverageData
cov, testsjson, root, out = sys.argv[1:5]
fxdir = sys.argv[5] if len(sys.argv) > 5 else None
subdir = sys.argv[6] if len(sys.argv) > 6 else None
T = json.load(open(testsjson))['tests']
num = {t['id']: t['number'] for t in T}
byclass = collections.defaultdict(list)
for t in T:
    byclass[t['id'].rsplit('::', 1)[0]].append(t['number'])
d = CoverageData(cov); d.read()
root = os.path.abspath(root)
files = {}
unknown = set()
for f in d.measured_files():
    rel = os.path.relpath(f, root)
    lines = collections.defaultdict(set)
    imp = set()
    for ln, ctxs in d.contexts_by_lineno(f).items():
        for c in ctxs:
            if c == '':
                imp.add(ln); continue
            nid, phase = c.rsplit('|', 1)
            if phase == 'run' or phase == 'teardown':
                if nid in num: lines[ln].add(num[nid])
                else: unknown.add(nid)
            elif phase == 'setup':
                # setUpClass runs once per process; its effects reach every test of the class
                if nid in num:
                    lines[ln].update(byclass[nid.rsplit('::', 1)[0]])
                else: unknown.add(nid)
    files[rel] = (lines, imp)
if subdir:
    # lines run inside ProcessPoolExecutor children, attributed to the submitting test
    for fn in glob.glob(os.path.join(subdir, '*.jsonl')):
        for line in open(fn):
            o = json.loads(line)
            if o['t'] not in num:
                continue
            n = num[o['t']]
            for rel, lns in o['l'].items():
                if rel not in files:
                    files[rel] = (collections.defaultdict(set), set())
                for ln in lns:
                    files[rel][0][ln].add(n)
sets = {}
setlist = []
outfiles = {}
for rel, (lines, imp) in sorted(files.items()):
    m = {}
    for ln, s in lines.items():
        k = tuple(sorted(s))
        if k not in sets:
            sets[k] = len(setlist); setlist.append(list(k))
        m[ln] = sets[k]
    outfiles[rel] = {'l': {str(k): v for k, v in sorted(m.items())}, 'i': sorted(imp)}
fx = {}
if fxdir:
    for f in glob.glob(os.path.join(fxdir, '*.json')):
        for k, v in json.load(open(f)).items():
            nid, _, phase = k.rpartition('|')
            if nid not in num:
                continue
            targets = byclass[nid.rsplit('::', 1)[0]] if phase == 'setup' else [num[nid]]
            for p in v:
                fx.setdefault(p, set()).update(targets)
fx = {k: sorted(v) for k, v in fx.items()}
print('unknown contexts', len(unknown), list(unknown)[:5], file=sys.stderr)
print('files', len(outfiles), 'sets', len(setlist), 'fx', len(fx), file=sys.stderr)
with gzip.open(out, 'wt') as fh:
    json.dump({'sets': setlist, 'files': outfiles, 'fx': fx}, fh, separators=(',', ':'))
print('size', os.path.getsize(out), file=sys.stderr)
