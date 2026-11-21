#!/bin/sh
# Run an image the way the scorer does, on a set dir; print CPU usage (cgroup) and wall time.
# usage: tools/run_docker.sh <image> <set dir> <output.jsonl>
IMG=$1; SET=$(cd "$2" && pwd); OUT=$3
START=$(python3 -c 'import time;print(time.time())')
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$SET/commits":/data/commits:ro \
  --volume "$SET/tests.json":/data/tests.json:ro \
  --entrypoint sh "$IMG" -c 'python /app/selector.py; s=$?; grep usage_usec /sys/fs/cgroup/cpu.stat >&2; exit $s' > "$OUT" 2> "$OUT.err"
RC=$?
END=$(python3 -c 'import time;print(time.time())')
echo "exit=$RC wall=$(python3 -c "print(round($END-$START,2))")s $(grep usage_usec "$OUT.err")"
