# Shader warm-up: play order and byte-identical duplicates

- Date: 2026-10-21
- Branch: `perf/warm-order-dedup`
- Previous: [Benchmarks](2026-10-21-benchmarks.md)

## Problem

The packaging-time warm-up compiled the seed's files in alphabetical order, one file per cache entry. Two costs came from
that:

1. **The title shaders were scattered through the run.** The first frame waits for the title list, not for the 20,000
   gameplay shaders. Alphabetical order delayed the few hundred files that actually gate the first start until the
   compiler reached their names.
2. **The seed contains byte-identical files.** In the 1.4.3 seed, 2,071 of 22,643 `.metal` files have a duplicate with
   the same text. Metal keys a library by its text and compile options, so the second copy is a guaranteed cache hit —
   but it still paid a compiler call, a worker slot and scheduler time.

There was also no shared definition of "the game's order": `nvn_mtl.m` walks the title list first, then the manifest,
while the packaging tool knew none of that.

## What changes

### The tool (`games/totk/tools/warm_metal_cache.m`)

- `--list <file>` reads an ordered list (one absolute path per line, `#` comments and blank lines ignored) instead of
  listing the folder alphabetically.
- Before compiling, each file is hashed with streaming SHA-256 (`file_digest`). A digest already seen is counted as a
  duplicate and skipped; a file that cannot be read is counted as missing. The JSON report gains `unique`, `duplicates`
  and `missing`.
- The compile loop and the `--verify` sample are unchanged: this increment does not add a budget or memory-pressure
  control.

### The build (`build_app.py`)

- `pso_names` / `pso_keys` parse the game's `pso_title.tsv` and `pso_manifest.tsv` rows: `S`/`C` rows name a file
  (`<key>-<answers>_b`), `R` rows pack two pipeline file names in hex. The answers hash in a name belongs to the chain
  that wrote the list, so the **key** (the first 64-hex field) is what finds the seed's file; all answer variants of that
  key are taken.
- `warm_order(seed, build)` returns the files in the order the game meets them:
  1. the title list (`pso_title.tsv`) — the first frame waits only for these;
  2. the game's manifest (`pso_manifest.tsv`);
  3. the seed's own manifest;
  4. everything else.
  Inside each group the largest file comes first, so a 1 MB shader does not become a long tail of one.
- `step_warm_metal` writes that order to `logs/warm-order.txt` and passes `--list` to the tool. The order file stays in
  the build logs, so a run can be audited after the fact. The success message now reports duplicates ("N files are
  byte-identical to another one and were not compiled twice").

## Tests

- `WarmOrderTests` (two tests) builds a synthetic seed and lists: title rows plus an `R` row, a game manifest, a seed
  manifest with a missing file, and checks the exact order and that the largest file comes first inside a group. A second
  test checks that with no lists every file is still ordered, largest first.
- The tool test now creates a byte-identical pair and checks `files=4, unique=3, duplicates=1, compiled=1, skipped=1,
  failed=1`; a `--list` test checks that only the listed files are compiled.
- The step test checks the new command line (`--list <order file>`) and the duplicates message.
- Full suite on this branch: 268 tests, OK (`games/totk/.venv/bin/python -m unittest discover -s tests`, series base
  `30a1b93`). The tool tests build the real Objective-C source with `xcrun clang` and run it against small Metal sources.

## Measurements

From the 1.4.3 seed used by the build:

| Quantity | Value |
|---|---|
| `.metal` files in the seed | 22,643 |
| byte-identical duplicates | 2,071 (9.1%) |
| unique files to compile | 20,572 |

These counts are from the seed on the original series base `4fdda41`. The upstream shader list has grown since (more
variants in `games/totk/assets/shader_list.tsv`), so a seed prepared on `30a1b93` holds more files. The mechanism is the
same (text-identical files are skipped, and the game's lists set the order); the new-base measurement is in
[Re-measured on the new base](2026-10-21-remeasure-30a1b93.md) (23,353 files, 21,134 unique, 2,219 duplicates).

Across the full measured warm-up (recorded in the next increment), those unique files split into 17,423 cold compiles and
3,149 store hits. The average cold compile in that run cost about 0.58 s of worker time, so skipping the 2,071 duplicates
removed roughly 1,200 worker-seconds — about 200 s at six threads, or ten minutes at the two threads this branch still
uses. That is an estimate from the measured average, not a separate A/B run.

The order has no standalone timing number: its value is that the title shaders finish first, which matters for the first
frame and for the warm-rebuild check added in the next increment.

## Trade-offs

- Reading and hashing every file before compiling adds a pass over the seed (about 1 GB). It is I/O-bound and an order of
  magnitude cheaper than the compiles it can skip.
- Deduplication is by content, not by name: two files with the same text but different names are one entry, which is
  exactly how Metal keys them. If the game ever needed both names for different compile options, this would be wrong;
  today the options are identical for every seed file.
- The order depends on lists produced by the game. If they are absent (an older work folder, a hand-built seed), the tool
  still compiles every file, largest first — a safe fallback, not an error.
- `--list` is a contract between `build_app.py` and the tool. The tool remains usable without it, and the list is kept in
  the logs so a failed run can be reproduced.
