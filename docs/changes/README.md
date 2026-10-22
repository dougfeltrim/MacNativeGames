# Change series: M1 support and fast shader warm-up

This directory records the work that turned the M1 base (8 GB, Apple7 GPU) into a first-class target and cut the
packaging-time shader warm-up from about an hour to about half an hour, in reviewable increments. Each increment is a
branch stacked on the previous one; they are meant to be reviewed and merged in the order below.

The series starts from `origin/main` at `30a1b93` (2026-10-21, after the fork synced upstream). Nothing here changes
the iPhone path or the tier-2 (M2 and newer, 16 GB+) defaults.

| # | Date | Increment | Branch | What changes |
|---|------|-----------|--------|--------------|
| 1 | 2026-10-20 | [Design spec and implementation plan](2026-10-20-m1-design.md) | `docs/m1-design` | Docs only: the plan every later increment follows |
