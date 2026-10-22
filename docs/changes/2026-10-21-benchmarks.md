# Benchmarks: `TOTK_BENCH` and a log-to-table report

- Date: 2026-10-21
- Branch: `feat/bench`
- Previous: [Tier-1 preset](2026-10-21-m1-tier1-preset.md)
- Design: [§6](../superpowers/specs/2026-10-20-m1-improvements-design.md)

## Problem

The design's success criteria are measured, not assumed: a 60 FPS output on the M1 base, a shader step without heavy
swap, and no regression elsewhere. The repository had only M4 numbers, and reading them out of a game log by hand is
slow and easy to get wrong. A benchmark has to sample a fixed number of frames after the warm-up, with the camera
still, and produce numbers that can be compared across runs.

## What changes

1. **`TOTK_BENCH=<n>` (`hle_nvn.c`).** Opt-in. The game samples frames 301 to 300+n — the first 300-frame window is
   warm-up and is not part of the result — and flushes one line at the end:

   ```
   [bench] {"frame_ms":[...],"gpu_ms":[...],"present_ms":[...],"tier":1}
   ```

   The collection reuses the existing timing windows (`g_tw`, `g_tn`, `g_pres_iv`): when a window is about to be
   overwritten, its samples are copied first. `n` must be a multiple of the 300-frame window (a shorter request is
   rejected with a log line rather than silently rounded) and is capped at 8,192 frames. A malformed value means the
   benchmark stays off.
2. **`TOTK_BENCH_EXIT=1`.** After the flush, the game exits with `_exit(0)`. A scripted run therefore ends at a known
   point and measures a fixed scene instead of running until somebody closes the window.
3. **`games/totk/scripts/bench_report.py`.** Reads the last `[bench]` line of a log (scripted runs may append several)
   and prints one Markdown table row: date, scenario, tier, render size and MetalFX state (read from the game's own log
   markers), frame count, average FPS, p50/p95/p99 frame time, percentage over 16.7 ms, drops, and GPU p50/p95.
   `--append docs/m1-benchmarks.md` adds the row to the results table; `--scenario` and `--date` name the run.
   Percentiles use the game's own nearest-rank formula (`n*p//100`, capped at `n-1`) so the report agrees with the
   in-game summary.

## Tests

`tests/test_bench_report.py` covers the report on synthetic logs: the last bench line wins, percentiles and drop
counting use the game's formulas, the render size and MetalFX markers are read from the log, and a log without a
`[bench]` line is an error. The full suite ran on this branch with the project virtualenv
(`games/totk/.venv/bin/python -m unittest discover -s tests`): 264 tests, OK (on the series base `30a1b93`).

The game-side collection has no unit test (it lives in the renderer); it was exercised by the M1 runs whose numbers are
recorded in the benchmark documentation added by the later increments of this series. Those runs were made on the
original base `4fdda41`; the collection code is unchanged, but a run on `30a1b93` is needed to put numbers on the new
base.

## How to use it

```sh
TOTK_BENCH=900 TOTK_BENCH_EXIT=1 <launch the game>
games/totk/scripts/bench_report.py ~/Library/Logs/MacNativeGames/totk.log --scenario camp --append docs/m1-benchmarks.md
```

900 frames is three 300-frame windows: five seconds at 60 FPS or ten at 30. The camera and the save are whatever the
run starts with; the tool does not move the player or freeze the world, so scenario names must describe the save and
the spot.

## Trade-offs

- Off by default: when `TOTK_BENCH` is unset, `bench_frames()` returns 0 and `bench_collect()` returns immediately. The
  only permanent cost is the static sample arrays (about 200 KB), not per-frame work.
- GPU spans arrive eight frames late, so `gpu_ms` can have a few samples fewer than `frame_ms` and is not aligned per
  frame. The report uses it for the GPU distribution, not for per-frame correlation; this is stated in the code.
- `_exit(0)` skips normal shutdown. It exists only for scripted measurements; a normal run never calls it.
- The result is only as fixed as the scene. For comparable numbers, use the same save, the same spot and the same
  camera; the report records the date and scenario so that a reader can see when that was not the case.
