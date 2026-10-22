/* TOTK_TIER probe: the tier classification and the totk_tier_default contract, without booting the game.
   The checks are internal (the expected tier depends on the machine): tier in {1,2}, an explicit env string always
   wins, and the defaults that come back match the tier that was reported. On the M1 base 8 GB (the validation
   machine) the JSON must show tier 1. */
#include <stdio.h>
#include <string.h>
#include "totk_tier.h"

int main(void) {
    int t = totk_tier();
    const char *d_render = totk_tier_default(NULL, "1280x720", NULL);
    const char *d_mfx = totk_tier_default(NULL, "1", "0");
    const char *env = totk_tier_default("900", "1280x720", NULL);
    int ok = (t == 1 || t == 2) && env && !strcmp(env, "900");
    if (t == 1) ok = ok && d_render && !strcmp(d_render, "1280x720") && d_mfx && !strcmp(d_mfx, "1");
    else        ok = ok && d_render == NULL && d_mfx && !strcmp(d_mfx, "0");
    printf("{\"tier\":%d,\"default_render\":\"%s\",\"default_mfx\":\"%s\",\"env_wins\":\"%s\",\"ok\":%d}\n",
           t, d_render ? d_render : "null", d_mfx, env, ok);
    return ok ? 0 : 1;
}
