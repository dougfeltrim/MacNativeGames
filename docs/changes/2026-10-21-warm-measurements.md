# Measured results: M1 warm-up and first launch

- Date: 2026-10-21
- Branch: `docs/warm-measurements`
- Previous: [Warm-up budget and verification](2026-10-21-warm-budget-verify.md)

## Problem

The warm-up increments produced numbers in build output and game logs, but the repository still only had M4 figures. A
reader could not tell what the M1 base actually does, and a future change had no baseline to compare against.

## What changes

Two existing documents are brought up to date:

1. **`docs/first-launch-shaders.md`** gains a section on the warm-up speed-up: what changed (order, dedup, budget,
   hot-cache skip, measured threads, `caffeinate`), the thread scan table, the eviction check, the full build timing, the
   hot rebuild, and the first launch after the build. It keeps the method and background that made the numbers
   interpretable.
2. **`docs/m1-benchmarks.md`** is added as the numbers page: machine, seed size, thread scan, eviction check, full build
   stages, hot rebuild, and first-launch table. The in-game FPS section is explicitly marked as not yet measured with
   `TOTK_BENCH`; it points at `docs/performance-followups.md` for the measurement rules instead of inventing numbers.

## Headline numbers

| Measurement | Result |
|---|---|
| Seed | 22,643 files, 20,572 unique contents |
| Thread scan | 2/4/6/8 threads: 5.8 / 8.2 / 9.9 / 8.4 shaders per second; 8 threads added 54 MB of swap |
| Full build warm step | 1,960 s (33 min), against about an hour with the old two-thread default |
| Main warm-up | 1,692 s, 17,423 cold compiles + 3,149 hits, 10,092 s of compile time summed, 31 s throttled |
| Post-check | 64/64 hits, median 0.18 ms |
| Second build, same place | 2.1 s, pre-check 64/64 hits, nothing compiled, signature valid |
| Shared store | 230 MB → 548 MB (limit ~1.08 GB); earliest entries survived |
| First launch | first frame 32.9 s; 22,643 shaders + 182 pipelines warmed in 36.3 s with 2 threads; 22,801 runtime compile calls, all hits |

All numbers above were measured on the original series base `4fdda41`. The upstream shader list grew before the series
was restacked onto `30a1b93`, so the seed on the new base is larger and every absolute count needed a fresh run; it is
in [Re-measured on the new base](2026-10-21-remeasure-30a1b93.md) (23,353 files, 21,134 unique; 4-minute build with a
warm store; first frame 3.1–8.1 s). The mechanisms and the comparison against the old two-thread default are
unchanged.

## Why these are trustworthy

- The full build ran on the validation machine (M1, 8 GB) with a cold store and six threads; the report comes from the
  tool's own JSON counts, not from estimates. The one estimate in the series (the duplicate saving) is labeled as such in
  the order/dedup document.
- The hot-cache path is measured separately: same app location, pre-check hits, 2.1 s total, no compile.
- The first launch was measured with the store already warm, which is the state a player gets after a build on the same
  Mac. The 32.9 s first frame includes a 14 s one-time import of 64,858 seed files and is not a shader-compile figure.
- The FPS page says plainly that `TOTK_BENCH` results are pending. The series does not present the 60 FPS goal as
  achieved.

## Trade-offs

- One machine (8 GB M1, macOS 26, Command Line Tools only). The M4 reference numbers remain in the older documents; no
  M2/M3/M4 run was repeated for this series.
- Build timings include unrelated stages (the 2,045 s macOS scan of the 16 GB app); they are listed separately so nobody
  attributes them to shaders.
- The first-launch table depends on the save and on how many shaders the player's session encounters. It records what
  happened, not a guaranteed bound.
- The numbers are dated and tied to the 1.0.0 seed. A different game version changes the seed size and therefore every
  count; the documents say so.
