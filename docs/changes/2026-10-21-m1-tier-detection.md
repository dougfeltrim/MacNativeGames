# M1 tier detection

- Date: 2026-10-21
- Branch: `feat/m1-tier-detection`
- Previous: [M1 design](2026-10-20-m1-design.md)
- Design: [§3](../superpowers/specs/2026-10-20-m1-improvements-design.md)

## Problem

Every later change in the series needs one answer to "is this an entry Mac?". Without it, a preset would have to be
duplicated in each knob, and the build could not tell an 8 GB Mac from a 32 GB one. The build already knew the core
count but not the RAM; the game knew neither in one place.

## What changes

1. **`build_app.phys_ram_bytes()`** reads `sysctl -n hw.memsize`. On any error it returns `None`, and the caller keeps
   today's behaviour. It is a small function, but it is the build's only source of truth for RAM and gets its own tests.
2. **`games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.{h,m}`** adds the shared classifier:
   - tier 1 = the GPU does not support the Apple7 family, or the Mac has less than 12 GB of RAM;
   - tier 2 = everything else, and always on iOS;
   - `TOTK_TIER=1|2` overrides detection for A/B tests on one machine;
   - the decision is made once (`dispatch_once`) and cached.
3. **`totk_tier_default(env, def_t1, def_t2)`** is the only API consumers use: it returns the environment string when
   set (including `"0"`), otherwise the tier's default. Parsing stays with each knob (`atoi`, `strcmp`, `sscanf`), so
   this module never changes how a value is interpreted.
4. **`make probe-tier`** (`scripts/tier_probe.c`) checks the contract without booting the game: the tier is 1 or 2, an
   explicit environment string wins, and the returned defaults match the reported tier.

The renderer is shared by TotK and BotW, so both games get the classifier from one place.

## Tests

- `PhysicalRamTests` (two tests): `sysctl` is called with the documented arguments and its number is returned; a failing
  `sysctl` yields `None`.
- `make probe-tier` on the validation machine (M1, 8 GB) prints:

```json
{"tier":1,"default_render":"1280x720","default_mfx":"1","env_wins":"900","ok":1}
```

The probe exits non-zero when `ok` is false, so the target can be used in a shell script.

The full suite ran on this branch with the project virtualenv
(`games/totk/.venv/bin/python -m unittest discover -s tests`): 258 tests, OK (on the series base `30a1b93`). The system
Python 3.15 lacks the `cryptography` module, so it cannot import the container tests; that is an environment difference,
not a code failure.

## Trade-offs

- The RAM clause puts newer 8 GB Macs (for example an M2 Air) on tier 1 even though their GPU is not Apple7. That is
  deliberate: the build and warm-up defaults exist to protect 8 GB machines, and their GPU is still entry-level.
- `TOTK_TIER` accepts anything: `1` means tier 1, every other value means tier 2. It is a test override, not a
  user-facing setting, and the simple rule is easy to reason about.
- Detection happens once. A machine that changes memory pressure later does not change tier; the preset is a startup
  default, not an adaptive controller.

## Not in this increment

No renderer or build default uses the classifier yet. The next increment wires it into MetalFX, the render resolution
and the game's warm-up threads; the build's thread choice lands with the warm-up work.
