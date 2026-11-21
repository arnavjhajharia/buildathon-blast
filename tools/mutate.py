"""Make a set of made-up single-commit mutations of sqlglot at the pinned commit,
in the same shapes as the sample commits (operator swaps, constants, booleans,
added comments, corrupted fixture lines, and mixes of these).

usage: mutate.py <sqlglot checkout> <out set dir> <count> <seed>
Writes <out>/commits/<id>.patch and <out>/kinds.json. Labels come from truth.py.
"""

import difflib
import json
import os
import random
import re
import sys
import tokenize
import io

SRC, OUT, COUNT, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(SEED)

COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Keep in sync with the generator",
    "# Mirrors the behavior of the reference implementation",
    "# TODO: revisit",
    "# Order matters here",
]

PY_FILES = sorted(
    os.path.relpath(os.path.join(d, f), SRC)
    for d, _, fs in os.walk(os.path.join(SRC, "sqlglot"))
    for f in fs
    if f.endswith(".py")
)
FX_FILES = sorted(
    os.path.relpath(os.path.join(d, f), SRC)
    for d, _, fs in os.walk(os.path.join(SRC, "tests", "fixtures"))
    for f in fs
    if f.endswith(".sql")
)


def read(p):
    with open(os.path.join(SRC, p), encoding="utf-8") as fh:
        return fh.read()


def code_lines(text):
    """Line numbers (1-based) whose tokens are code, not inside strings/comments only."""
    ok = set()
    strings = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                if tok.start[0] != tok.end[0]:
                    strings.update(range(tok.start[0], tok.end[0] + 1))
            elif tok.type == tokenize.OP or tok.type == tokenize.NAME or tok.type == tokenize.NUMBER:
                ok.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return ok - strings


OPS = [
    (r" and ", " or "),
    (r" or ", " and "),
    (r" is not None", " != None"),
    (r" is None", " == None"),
    (r" is not ", " is "),
    (r" is (?!not)", " == "),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" < ", " <= "),
    (r" <= ", " < "),
    (r" > ", " >= "),
    (r" >= ", " > "),
    (r"\bnot ", ""),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"(?<![\w.])(\d+)(?![\w.])", None),  # integer +1
]


def code_mutation(path):
    text = read(path)
    lines = text.split("\n")
    cl = sorted(code_lines(text))
    sites = []
    for ln in cl:
        s = lines[ln - 1]
        stripped = s.lstrip()
        if stripped.startswith("#") or stripped.startswith("import ") or stripped.startswith("from "):
            continue
        # crude: skip the part of the line after a comment marker
        code = s.split("#", 1)[0] if "'" not in s and '"' not in s else s
        for i, (pat, rep) in enumerate(OPS):
            for m in re.finditer(pat, code):
                # do not touch text inside quotes on this line
                before = s[: m.start()]
                if before.count('"') % 2 or before.count("'") % 2:
                    continue
                sites.append((ln, i, m.start(), m.end()))
    if not sites:
        return None
    ln, i, a, b = rng.choice(sites)
    s = lines[ln - 1]
    pat, rep = OPS[i]
    if rep is None:
        n = int(s[a:b])
        rep = str(n + 1)
    new = s[:a] + rep + s[b:]
    if new == s:
        return None
    lines2 = list(lines)
    lines2[ln - 1] = new
    return path, text, "\n".join(lines2)


def comment_mutation(path):
    text = read(path)
    lines = text.split("\n")
    cl = sorted(code_lines(text))
    if not cl:
        return None
    ln = rng.choice(cl)
    s = lines[ln - 1]
    # only at the start of a statement line (previous line is not a continuation)
    prev = lines[ln - 2] if ln >= 2 else ""
    if prev.rstrip().endswith("\\"):
        return None
    indent = s[: len(s) - len(s.lstrip())]
    lines2 = lines[: ln - 1] + [indent + rng.choice(COMMENTS)] + lines[ln - 1 :]
    return path, text, "\n".join(lines2)


TOKENS = ["42", "NULL", "0", "x", "1"]


def fixture_mutation(path):
    text = read(path)
    lines = text.split("\n")
    cand = [
        i
        for i, l in enumerate(lines)
        if l.strip() and not l.startswith("#") and not l.startswith("--") and len(l) > 3
    ]
    if not cand:
        return None
    i = rng.choice(cand)
    s = lines[i]
    kind = rng.randrange(4)
    if kind == 0:  # insert token before a word
        words = [m.start() for m in re.finditer(r"\b\w", s)]
        p = rng.choice(words)
        s2 = s[:p] + rng.choice(TOKENS) + " " + s[p:]
    elif kind == 1:  # delete one character
        p = rng.randrange(len(s))
        s2 = s[:p] + s[p + 1 :]
    elif kind == 2:  # append token after a word
        words = [m.end() for m in re.finditer(r"\w\b", s)]
        p = rng.choice(words)
        s2 = s[:p] + rng.choice(TOKENS) + " " + s[p:]
    else:  # insert a character
        p = rng.randrange(len(s))
        s2 = s[:p] + rng.choice("a x") + s[p:]
    if s2 == s:
        return None
    lines[i] = s2
    return path, text, "\n".join(lines)


def udiff(path, old, new):
    d = difflib.unified_diff(
        old.split("\n"), new.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3
    )
    body = "\n".join(d)
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body + "\n"


def make(kind):
    for _ in range(200):
        if kind == "code":
            r = code_mutation(rng.choice(PY_FILES))
        elif kind == "comment":
            r = comment_mutation(rng.choice(PY_FILES))
        else:
            r = fixture_mutation(rng.choice(FX_FILES))
        if r:
            return r
    raise SystemExit("no mutation")


os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
kinds = {}
weights = [("code", 17), ("fixture", 8), ("comment", 3), ("multi", 3)]
pool = [k for k, w in weights for _ in range(w)]
for n in range(COUNT):
    kind = rng.choice(pool)
    cid = f"m{SEED:02d}{n:03d}"
    if kind == "multi":
        parts = {}
        for sub in rng.sample(["code", "comment", "fixture"], rng.choice([2, 3])):
            for _ in range(20):
                p, old, new = make(sub)
                if p not in parts:
                    parts[p] = (old, new)
                    break
        patch = "".join(udiff(p, o, nw) for p, (o, nw) in sorted(parts.items()))
    else:
        p, old, new = make(kind)
        patch = udiff(p, old, new)
    with open(os.path.join(OUT, "commits", cid + ".patch"), "w") as fh:
        fh.write(patch)
    kinds[cid] = kind
with open(os.path.join(OUT, "kinds.json"), "w") as fh:
    json.dump(kinds, fh, indent=0)
print(f"wrote {COUNT} mutations to {OUT}")
