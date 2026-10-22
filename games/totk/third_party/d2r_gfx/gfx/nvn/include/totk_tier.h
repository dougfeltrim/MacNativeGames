/* TOTK: the tier-1 preset selector (totk_tier.m). The renderer is shared by TotK and BotW, so both games use it. */
#ifndef TOTK_TIER_H
#define TOTK_TIER_H
/* 1 = entry Macs (an Apple7-class GPU or less than 12 GB of RAM), 2 = the rest; iOS is always 2.
   TOTK_TIER=1|2 overrides the detection (A/B tests without changing hardware). */
int totk_tier(void);
/* The env string when it is set (even "0"), otherwise the tier's default: def_t1 on tier 1, def_t2 otherwise (may be
   NULL). Callers keep their own parsing: atoi for sizes, strcmp for flags. */
const char *totk_tier_default(const char *env, const char *def_t1, const char *def_t2);
#endif
