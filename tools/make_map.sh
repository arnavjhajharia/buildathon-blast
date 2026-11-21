#!/bin/sh
# Build the coverage + fixture map at the pinned sqlglot commit.
# usage: tools/make_map.sh <sqlglot checkout> <tests.json> <out.json.gz> [xdist workers]
set -e
SRC=$(cd "$1" && pwd); TJ=$(cd "$(dirname "$2")" && pwd)/$(basename "$2"); OUT=$3; N=${4:-6}
TOOLS=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
cd "$SRC"
rm -f .coverage .coverage.*
FXOUT=$WORK/fx SUBCOV_OUT=$WORK/sub SKIP_INTEGRATION=0 PYTHONPATH=.:$TOOLS \
  python -m pytest -q -n "$N" -p no:cacheprovider -p fxplugin -p subcov \
  --cov=sqlglot --cov-context=test --cov-report= tests | tail -3 || true
cd - >/dev/null
python "$TOOLS/buildmap.py" "$SRC/.coverage" "$TJ" "$SRC" "$OUT" "$WORK/fx" "$WORK/sub"
