# Blast Radius entry

For each patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), pick the tests
that can fail and split them over workers.

## How it decides

All evidence comes from the pinned sqlglot commit, never from any patch:

- `data/covmap.json.gz`: per-test line coverage of `sqlglot/` (coverage.py test contexts, plus
  lines run inside `ProcessPoolExecutor` children and pickling, attributed to the test that
  submitted the work), and which fixture files each test opens.
- The original sqlglot sources, downloaded in the `Dockerfile` at the pinned commit.

For each patch `selector.py`:

1. Applies the patch in memory. If the module's syntax tree is unchanged (comments, blank
   lines), the change cannot alter behaviour and selects nothing for that file.
2. Maps each changed line to its enclosing statement with `ast`. Inside a function: the tests
   that executed that statement. A signature or decorator: the tests that ran the function.
   Module or class level code (runs at import): every test that ran code in that file.
3. Fixture files: the tests that opened the file.
4. Anything it does not understand (new, deleted or renamed files, patches that do not apply):
   every test.
5. Packs the chosen tests over 1 to 8 workers (longest first onto the least loaded worker) and
   keeps the worker count with the lowest busiest-worker time plus 2 s per worker.

`tools/` holds the map builder (`make_map.sh`, `buildmap.py`, pytest plugins), a mutation
generator and labeller for testing on made-up changes (`mutate.py`, `truth.py`), and scoring
helpers. They run only at development time.

## Open-source libraries

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT): the code under test; its sources are read
  at run time.
- [coverage.py](https://github.com/nedbat/coveragepy) (Apache 2.0), [pytest](https://pytest.org)
  (MIT), [pytest-cov](https://github.com/pytest-dev/pytest-cov) (MIT) and
  [pytest-xdist](https://github.com/pytest-dev/pytest-xdist) (MIT): used to build the map.
- Python standard library only at run time.

## License

MIT, see `LICENSE`.
