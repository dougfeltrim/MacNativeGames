# Change series: M1 support and fast shader warm-up

This directory records the work that turned the M1 base (8 GB, Apple7 GPU) into a first-class target and cut the
packaging-time shader warm-up from about an hour to about half an hour, in reviewable increments. Each increment is a
branch stacked on the previous one; they are meant to be reviewed and merged in the order below.

The series starts from `origin/main` at `30a1b93` (2026-10-21, after the fork synced upstream). Nothing here changes
the iPhone path or the tier-2 (M2 and newer, 16 GB+) defaults.

| # | Date | Increment | Branch | What changes |
|---|------|-----------|--------|--------------|
| 1 | 2026-10-20 | [Design spec and implementation plan](2026-10-20-m1-design.md) | `docs/m1-design` | Docs only: the plan every later increment follows |
| 2 | 2026-10-21 | [M1 tier detection](2026-10-21-m1-tier-detection.md) | `feat/m1-tier-detection` | `totk_tier` classifies Apple7/<12 GB as tier 1; `phys_ram_bytes`; `make probe-tier` |
| 3 | 2026-10-21 | [Tier-1 preset](2026-10-21-m1-tier1-preset.md) | `feat/m1-tier1-preset` | 720p scene, MetalFX on, 2 warm-up threads; BotW 720p branch; tier 2 unchanged |
| 4 | 2026-10-21 | [Benchmarks](2026-10-21-benchmarks.md) | `feat/bench` | `TOTK_BENCH` samples fixed frames into one JSON line; `bench_report.py` prints/appends a table row |
| 5 | 2026-10-21 | [Warm-up order and dedup](2026-10-21-warm-order-dedup.md) | `perf/warm-order-dedup` | Title list first, largest-first groups, SHA-256 dedup of 2,071 identical files |
