# Shader warm-up: measured threads, budget and hot-cache skip

- Date: 2026-10-21
- Branch: `perf/warm-budget-verify`
- Previous: [Warm-up order and dedup](2026-10-21-warm-order-dedup.md)

## Problem

After the order and dedup work, the warm-up still had three problems on an 8 GB M1:

1. **Two threads was safe, not fast.** It avoided the heavy swap of one compiler per core, but at 5.8 shaders a second
   the 20,572 unique files take about an hour. The machine has six useful compiler slots; the design only knew that eight
   swapped.
2. **Every rebuild repeated the whole compile**, even when the Mac's store already held the title shaders. A second build
   in the same place should be nearly free.
3. **The store has a size limit (~1.08 GB) and evicts.** A run that compiles more than the store holds can push out the
   entries it just made. There was no way to cap a run or to tell how much of the report was cold compiles versus hits.

## What changes

All of it is in `build_app.py` and `games/totk/tools/warm_metal_cache.m`; the game runtime is untouched.

1. **Measured thread count.** Below 16 GB, the build uses `min(6, cpu_count)`. `TOTK_BUILD_WARM_THREADS` still wins with
   an explicit count (`0` and malformed values fall back to the RAM default). The chosen count is printed with the
   measurement that justified it.
2. **Memory-pressure gate in the tool.** A `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` handler caps concurrency: `WARN` halves
   the workers, `CRITICAL` drops to one, `NORMAL` restores the target. Workers wait on an `NSCondition` instead of piling
   up in the Metal compiler service. Waiting time is reported as `throttled_seconds`.
3. **Budget.** `--budget <n>` counts only cold compiles (a compile taking 5 ms or more; a store hit does not count) and
   stops when the budget is reached. The JSON report gains `cold`, `hits`, `budget_stopped` and `remaining`, and the build
   explains that the rest is left to the game's own background warm-up. `TOTK_BUILD_WARM_BUDGET` overrides it;
   `WARM_BUDGET = 0` (compile everything) remains the default because 20,572 libraries occupy about 390 MB of the
   ~1.08 GB store.
4. **Hot-cache skip.** Before compiling, the build runs `--verify 64 --list <order>` on the title group. When at least 97%
   of the 64 are hits, the step ends without compiling anything. On a cold store those 64 are the largest files of the
   title group and are compiled at the very start of the main run anyway, so the pre-check moves work, it does not
   duplicate it.
5. **Honest counts.** The tool separates `compiled` (every compile attempt) into `cold` and `hits` using the 5 ms
   threshold, and the build's success line reports both. The verify process warms the compiler connection before the
   sample, so the first request's connection setup is not counted as a slow shader.
6. **`caffeinate -i`** wraps the compile, so an idle sleep cannot stop the step.

## Measurements (8 GB M1, macOS 26, cold store)

These numbers were measured on the original series base `4fdda41`. The upstream shader list has grown since, so a seed
prepared on `30a1b93` holds more files; the thread ranking, the eviction behaviour and the hot-cache skip are properties
of the tool. The new-base measurement is in [Re-measured on the new base](2026-10-21-remeasure-30a1b93.md): 23,353
variants, hot pre-check 64/64, first-launch top-up 135.8 s and 17–23 s afterwards.

### Thread scan

1,500 new sources with random comments per setting, editor open on the machine:

| Threads | Shaders/s | Per shader | Store growth | Peak memory pressure | Swap |
|---:|---:|---:|---:|---|---|
| 2 | 5.8 | 344 ms | 28.4 MB | WARN | unchanged |
| 4 | 8.2 | 486 ms | 28.5 MB | WARN | unchanged |
| 6 | 9.9 | 604 ms | 28.5 MB | NORMAL | unchanged |
| 8 | 8.4 | 942 ms | 28.5 MB | WARN | +54 MB |

Six is the fastest; eight is slower and adds swap. Each library takes about 19 KB in the shared store.

### Eviction check

The batch compiled first (the two-thread setting) was sampled again after 4,500 more compiles: 64/64 hits, same for the
four-thread batch. The store was about 230 MB at the time (limit ~1.08 GB), so no eviction was observed; the budget is
still kept as the control for a fuller store.

### Full build (20,572 unique shaders, six threads, cold store)

| Stage | Time |
|---|---|
| Seed verification / reuse | 12 s |
| Assembly, signing, macOS first scan (`--check`) | 2,045 s (the system scanning the 16 GB app; unrelated to shaders) |
| Pre-check (64 title shaders) | 262 s cold; one largest source took 216 s alone |
| Main warm-up | 1,692 s (17,423 cold, 3,149 hits; compile time summed 10,092 s; throttled 31 s) |
| Post-check (new process) | 64/64 hits, median 0.18 ms, max 0.74 ms |

The whole warm step (pre-check + main + post-check) took **1,960 s (33 minutes)**, against about an hour for the old
two-thread setting. The shared store grew from 230 MB to 548 MB, and the earliest entries were still there at the end.

**Second build in the same place:** pre-check 64/64 hits (0.19 ms), whole step **2.1 s** including writing the 22,643-line
order file; nothing compiled; the tool was removed from the app and `codesign --verify --deep --strict` passed.

### First launch with the warmed store

The game's own background warm-up uses two threads on tier 1:

| Item | Result |
|---|---|
| GPU tier line | tier 1: 720p + MetalFX (`TOTK_TIER=2` turns it off) |
| First presented frame | 32.9 s after process start (about 14 s is the first-launch import of 64,858 seed files) |
| Startup critical pipelines ready | 8 ms wait (the title list's names carry the writer's answers hash and do not match the seed, so those rows were never pre-warmed by name; they compiled on demand and all hit) |
| Pipeline warm-up complete | 22,643 shaders + 182 pipelines, 2 threads, 36.3 s |
| Runtime compile calls | 22,801, shared store grew 0.2 MB — every call was a hit |

## Tests

The final test file covers the new behaviour: the pre-check and its 97% threshold, the explicit thread count winning over
the RAM default, the budget from the environment, the duplicates/cold/hits messages, and the real tool's `--list` and
budget stop (`TOTK_BUILD_WARM_BUDGET`, `TOTK_BUILD_WARM_THREADS`, `--verify` with a list). The tool tests build the
Objective-C source with `xcrun clang` and run it on small Metal sources.

Full suite on this branch: 271 tests, OK (`games/totk/.venv/bin/python -m unittest discover -s tests`, series base
`30a1b93`).

## Trade-offs

- Six threads uses more memory than two, but the measured run grew no swap and was 71% faster. The memory-pressure gate
  is the safety net for machines that are already busy (31 s of throttling in the full build).
- The pre-check costs 262 s on a cold store. It pays for itself on every rebuild (2.1 s instead of an hour) and does not
  add compiles: the same 64 shaders would be at the head of the main run.
- The budget defaults to unlimited. It exists to protect the store on machines with many other Metal programs, not to
  shorten a normal build; capping it would push compiles into the player's first session.
- `caffeinate -i` only prevents idle sleep during the build step; it does not keep the machine awake afterwards.
- `step_shader_libs` (compile the seed into `shader-libs` inside the app with the Xcode Metal toolchain) was still a
  stub on the original base, and this Mac has no such toolchain, so those command lines could not be validated here. The
  new base `30a1b93` implements it; `step_warm_metal` already checks for those libraries and skips the store warm-up
  when every seed shader has one. Removing the per-machine store entirely is still the toolchain path.
