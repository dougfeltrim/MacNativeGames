# Tier-1 preset: 720p scene, MetalFX and two warm-up threads

- Date: 2026-10-21
- Branch: `feat/m1-tier1-preset`
- Previous: [M1 tier detection](2026-10-21-m1-tier-detection.md)
- Design: [§4 and §5](../superpowers/specs/2026-10-20-m1-improvements-design.md)

## Problem

The M1 base inherited defaults written for an M4 and for iOS: the scene rendered at the game's own size, MetalFX was off,
and the shader warm-up and the background shader warm-up used one thread per core. On an 8 GB M1 that means less GPU
headroom than the defaults assume, memory pressure during compilation, and a machine that reaches its thermal limit
sooner than it needs to.

## What changes

The tier from the previous increment is now wired into every default. In each case the explicit environment variable
still wins, and tier 2 (M2 and newer, 16 GB+) keeps exactly the previous behaviour.

1. **Build (`build_app.py`, `step_warm_metal`).** Below 16 GB of RAM the tool runs with `2` compiler threads, and the
   build logs why plus the Metal toolchain tip (`xcodebuild -downloadComponent MetalToolchain`), which compiles the
   shaders into the app once and removes this per-machine step entirely.
2. **Game warm-up (`nvn_mtl.m`).** `TOTK_WARM_THREADS` keeps its explicit value; on tier 1 its default is now at most 2
   instead of cores − 2 (6 on an M1). The boot log prints one line with the tier, the GPU, the RAM and the preset.
3. **MetalFX (`totk_mfx_up.m`).** `TOTK_MFX_UPSCALE` now defaults to on for tier 1 and off for tier 2. The upscale is
   the game's docked compose: 1280x720 scene to the 1920x1080 canvas, replacing the game's own upscale for that draw.
4. **TotK render size (`game_patches.c`, `game_patches_v143.c`).** `TOTK_RENDER_RES` defaults to `1280x720` on tier 1 and
   to the game's own size on tier 2 (the `NULL` default). Values `0` and malformed strings keep their previous meaning.
5. **BotW (`game_patches_botw.c`).** The docked 1080p patch now has a 720p branch: tier 1 writes the 1280.0 constant,
   tier 2 keeps 1920x1080, and `TOTK_RENDER_RES=900` keeps the shipped 1600x900. The patch error message no longer says
   "1080p" because the target is now tier-dependent.

The renderer is shared, so TotK and BotW get the same preset.

## Validation

The design flagged one risky assumption: that `TOTK_RENDER_RES` actually governs the docked TotK scene. It does. On the
validation machine (M1, 8 GB), the game log shows:

```
[totk] render resolution patch: scene render size 1280x720 (3 instructions at main+0xc630bc, value on the data page)
[dm] [nvn-mtl] GPU tier 1（Apple M1，8 GB RAM）：tier 1 预设 720p + MetalFX（TOTK_TIER=2 关闭）
[dm] [nvn-mtl] [mfx-up] MetalFX spatial upscaling (TOTK_MFX_UPSCALE): on
[dm] [nvn-mtl] [mfx-up] docked: spatial scaler 1280x720 (format 92) -> 1920x1080 (format 92),
     perceptual colour processing, into the game's target; built in the background in 12 ms
[dm] [nvn-mtl] 后台预热：着色器库用 2 个线程编（TOTK_WARM_THREADS），进世界后最多 2 个
```

By frame 19,107 of that run the MetalFX path had replaced 16,201 of 16,201 docked upscales and left none un-upscaled
(`未放大 0 次`), so the fallback never had to take over in normal play. The scaler was built in the background (12–16 ms)
without a frame-long stall.

The full suite ran on this branch with the project virtualenv
(`games/totk/.venv/bin/python -m unittest discover -s tests`): 259 tests, OK (on the series base `30a1b93`).

The M1 log above was captured on the original series base `4fdda41`. The tier-1 changes in this branch are the same
ones that produced it, but the surrounding renderer now comes from `30a1b93`, which added pipeline and replay work; the
log is evidence for the preset logic, not a byte-for-byte run of this exact tree. A fresh M1 run on the new base is the
next measurement.

## Trade-offs

- 720p internal rendering is softer than 900p or 1080p native. It buys GPU headroom for 60 FPS and, as measured later,
  for a lower thermal load. `TOTK_RENDER_RES=1600x900` (or any `<width>x<height>`) and `TOTK_TIER=2` remain available.
- BotW support is early; the 720p branch is validated by the patch's instruction check and the shared renderer, not by a
  BotW benchmark. Its numbers will be added when BotW measurement happens.
- Two warm-up threads is the conservative value from the design. The warm-up increments replace it with a measured
  thread count once the compiler is instrumented; until then the goal is "no swap", not "fastest".
- The preset changes what the first frame waits for: at 720p the scene is cheaper, but the title-screen shaders still
  decide the first start. That work is addressed in the warm-up increments, not here.

## Not in this increment

No frame-rate measurement. The preset is validated for correctness and for taking effect; `TOTK_BENCH` and the benchmark
report arrive in the next increment so that the 60 FPS claim can be checked instead of assumed.
