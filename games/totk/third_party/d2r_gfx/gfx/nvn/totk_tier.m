// TOTK: which Macs get the tier-1 performance preset (spec docs/superpowers/specs/2026-10-20-m1-improvements-design.md).
// Tier 1 = the entry Macs: an Apple7-class GPU (M1/A14) or less than 12 GB of RAM (an 8 GB M2 counts too). Tier 1 gets
// lighter defaults (720p scene upscaled by MetalFX, two warm-up threads); tier 2 keeps today's defaults. An explicit
// environment variable always wins over the tier default, and iOS is always tier 2 (its handheld defaults are its own).
// totk_tier_default only picks the default string; each knob parses it as before (atoi / strcmp / sscanf).
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdlib.h>
#include "totk_tier.h"

static int g_tier;
static void tier_init(void *unused) {
    (void)unused;
#if TARGET_OS_IPHONE
    g_tier = 2;
#else
    const char *e = getenv("TOTK_TIER");
    if (e && *e) { g_tier = atoi(e) == 1 ? 1 : 2; return; }
    id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
    g_tier = (dev && ![dev supportsFamily:MTLGPUFamilyApple7]) || NSProcessInfo.processInfo.physicalMemory < 12ULL << 30 ? 1 : 2;
#endif
}
int totk_tier(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{ tier_init(NULL); });
    return g_tier;
}
const char *totk_tier_default(const char *env, const char *def_t1, const char *def_t2) {
    if (env && *env) return env;
    return totk_tier() == 1 ? def_t1 : def_t2;
}
