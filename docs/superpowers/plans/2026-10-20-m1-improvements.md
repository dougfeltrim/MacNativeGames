# Plano de implementação: melhorias para o M1 (preset por tier)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar ao M1 base (8 GB) 60 FPS em 1080p via MetalFX (cena interna 1280x720), uma etapa de shaders do build sem swap pesado, e benchmarks de M1 publicados — sem mudar o comportamento de M4+ nem de iOS.

**Architecture:** Um seletor de tier novo e minúsculo (`totk_tier` em `gfx/nvn`) é consultado pelos knobs existentes (`TOTK_RENDER_RES`, `TOTK_MFX_UPSCALE`, `TOTK_WARM_THREADS`) apenas para escolher o default — env explícita sempre vence e o parsing continua com cada knob. O build (`build_app.py`) lê a RAM física e reduz os threads do `warm_metal_cache` em máquinas < 16 GB. A medição reusa as janelas de timing existentes de `hle_nvn.c` (`g_tw`, `tsummary` de 300 em 300 frames): `TOTK_BENCH` coleta as amostras e imprime uma linha JSON única, que `bench_report.py` transforma em linha de tabela Markdown em `docs/m1-benchmarks.md`.

**Tech Stack:** Python 3 + unittest (build tooling e relatórios), C11/ObjC + Metal 4 (runtime e renderer), Make (build e probes). Nada novo além disso.

**Spec:** [docs/superpowers/specs/2026-10-20-m1-improvements-design.md](../specs/2026-10-20-m1-improvements-design.md) — o plano argumenta a partir da spec; implementadores devem lê-la junto.

## Global Constraints

- Plataforma: macOS 26+ (Metal 4). Nada muda para macOS 15 (fork comunitário Metal 3).
- **iOS intacto:** `TARGET_OS_IPHONE` → `totk_tier()` sempre 2; envs explícitas vencem em qualquer tier.
- **Tier 2 (M4+) intacto:** todas as mudanças de default são tier-gated; tier 2 recebe exatamente os defaults de hoje.
- **Renderer compartilhado:** TotK e BotW usam `games/totk/third_party/d2r_gfx`; tudo precisa compilar também com `-DGAME_BOTW=1`.
- **Fail-safe:** nenhuma mudança pode quebrar o build (fallback = caminho atual); MetalFX indisponível → upscale próprio do jogo (`g_unavailable`, tratamento existente).
- Constantes exatas: tier 1 = família de GPU ≤ Apple7 **ou** RAM < 12 GB; preset tier 1 = `1280x720` + MetalFX on; threads de build = 2 quando RAM < 16 GB; `TOTK_BENCH` múltiplo de 300 e ≤ 8192 frames; linhas de base 16,7 ms (frame) e 25 ms (drop de present).
- Testes: `python3 -m unittest discover -s tests -v` a partir da raiz do repo (o que o GitHub Actions roda); probes via targets em `games/totk/Makefile`; runs do jogo/probes de GPU passam por `scripts/dev/game_slot.py` (padrão `GAME_SLOT_RUN`).
- Máquina de validação: M1 base 8 GB (esta máquina). `make probe-tier` deve reportar tier 1 aqui.
- Pré-requisito de disco: um build completo novo do app exige ~45–60 GB livres no volume de dados (havia ~12 GB livres em 2026-10-20). O caminho de runtime binário (`make runtime`, `make boot`) não precisa disso. Antes de um build novo do app, liberar espaço ou reusar o app já construído.

---

## Fase 1 — Build e primeiro launch

### Task 1: `phys_ram_bytes()` no build

**Files:**
- Modify: `build_app.py` (nova função, antes de `step_warm_metal`, hoje linha 984)
- Test: `tests/test_warm_metal.py` (nova classe no fim do arquivo)

**Interfaces:**
- Produces: `phys_ram_bytes()` → `int | None` (bytes de RAM física via `sysctl -n hw.memsize`; `None` quando não dá para ler). A Task 2 usa isto para decidir os threads.

- [ ] **Step 1: Escrever o teste que falha**

Adicionar ao fim de `tests/test_warm_metal.py` (após `MetalCompilerTests`):

```python
class PhysicalRamTests(unittest.TestCase):
    """phys_ram_bytes: sysctl hw.memsize, None quando a leitura falha (o caller mantém o comportamento atual)."""

    def test_reads_sysctl(self):
        with mock.patch.object(build_app.subprocess, 'check_output', return_value='8589934592\n') as co:
            self.assertEqual(build_app.phys_ram_bytes(), 8 << 30)
        self.assertEqual(co.call_args.args[0], ['sysctl', '-n', 'hw.memsize'])

    def test_none_when_sysctl_fails(self):
        with mock.patch.object(build_app.subprocess, 'check_output',
                               side_effect=subprocess.CalledProcessError(1, 'sysctl')):
            self.assertIsNone(build_app.phys_ram_bytes())
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python3 -m unittest discover -s tests -p test_warm_metal.py -v`
Expected: `AttributeError: module 'build_app' has no attribute 'phys_ram_bytes'`

- [ ] **Step 3: Implementar**

Em `build_app.py`, imediatamente antes de `def step_warm_metal(tree, out, logs):` (linha 984):

```python
def phys_ram_bytes():
    """This Mac's physical RAM (sysctl hw.memsize), or None when it cannot be read (the caller keeps today's behaviour)."""
    try:
        return int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `python3 -m unittest discover -s tests -p test_warm_metal.py -v`
Expected: todos os testes passam (os existentes não tocam `check_output`, então não são afetados).

- [ ] **Step 5: Commit**

```bash
git add build_app.py tests/test_warm_metal.py
git commit -m "feat: read the Mac's physical RAM for the warm-up thread choice (sysctl hw.memsize)"
```

### Task 2: `step_warm_metal` com threads conscientes de RAM + dica do Metal toolchain

**Files:**
- Modify: `build_app.py:984-1035` (`step_warm_metal`, usar `phys_ram_bytes`)
- Test: `tests/test_warm_metal.py:31-54` (patchear `phys_ram_bytes` no helper `step`; novo teste low-RAM)

**Interfaces:**
- Consumes: `phys_ram_bytes()` da Task 1.
- Produces: `step_warm_metal` passa a invocar `warm_metal_cache` com `[tool, seed, '2']` quando RAM < 16 GB (a ferramenta já aceita o argumento, `tools/warm_metal_cache.m` linha 129: `threads = argc >= 3 ? atoi(argv[2]) : cores - 2`).

- [ ] **Step 1: Atualizar o helper de teste (o teste falha sem o patch)**

Em `tests/test_warm_metal.py`, no `with` do helper `step` (linhas 49-52), adicionar o patch para que os testes existentes simulem uma máquina de 32 GB (sem o patch, o sysctl real desta máquina de 8 GB faria todos os testes existentes falharem):

```python
        with mock.patch.object(build_app.subprocess, 'run', side_effect=system), mock.patch.object(build_app, 'run', side_effect=run), \
             mock.patch.object(build_app, 'clone', side_effect=lambda src, dst: shutil.copyfile(src, dst)), \
             mock.patch.object(build_app, 'say', side_effect=self.said.append), \
             mock.patch.object(build_app, 'phys_ram_bytes', return_value=32 << 30), \
             mock.patch.object(build_app, 'metal_cache_users', side_effect=lambda: next(cache_users)):
            build_app.step_warm_metal(self.tree, self.out, self.logs)
```

Run: `python3 -m unittest discover -s tests -p test_warm_metal.py -v`
Expected: todos os testes existentes passam (comportamento inalterado em 32 GB).

- [ ] **Step 2: Escrever o teste low-RAM que falha**

Adicionar à classe `StepWarmMetalTests` (após `test_runs_from_the_game_binarys_folder_and_leaves_nothing_there`):

```python
    def test_low_ram_uses_two_threads_and_points_at_the_toolchain(self):
        with mock.patch.object(build_app, 'phys_ram_bytes', return_value=8 << 30):
            calls = self.step(reports={
                'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0, 'threads': 2, 'seconds': 700.0},
                'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}})
        seed = str(self.app_build / 'shader-seed')
        self.assertEqual(calls, [[str(self.app_tool), seed, '2'], [str(self.app_tool), seed, '--verify', '64']])
        self.assertTrue(any('two compiler threads' in s for s in self.said))
        self.assertTrue(any('MetalToolchain' in s for s in self.said))
```

Run: `python3 -m unittest discover -s tests -p test_warm_metal.py -v`
Expected: FAIL — `calls` não tem o argumento `'2'` (o código ainda não o adiciona).

- [ ] **Step 3: Implementar**

Em `build_app.py`, `step_warm_metal`, substituir as linhas 1012-1014:

```python
        clone(tree / 'build/warm-metal-cache', tool)
        run([tool, seed], logs / 'warm-metal-cache.log')
        run([tool, seed, '--verify', '64'], logs / 'warm-metal-cache-verify.log', quiet=False)
```

por:

```python
        clone(tree / 'build/warm-metal-cache', tool)
        tool_args = [tool, seed]
        ram = phys_ram_bytes()
        if ram is not None and ram < 16 << 30:
            tool_args.append('2')   # 8 GB Macs swap with one compiler per core (about 25 minutes on an 8 GB M1)
            say("   8 GB Mac: two compiler threads so this step does not swap")
            say("   tip: an Xcode with the Metal toolchain (xcodebuild -downloadComponent MetalToolchain) compiles the "
                "startup shaders into the app instead (step_shader_libs), and this step is skipped entirely")
        run(tool_args, logs / 'warm-metal-cache.log')
        run([tool, seed, '--verify', '64'], logs / 'warm-metal-cache-verify.log', quiet=False)
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `python3 -m unittest discover -s tests -p test_warm_metal.py -v`
Expected: todos os testes passam, incluindo o novo low-RAM.

- [ ] **Step 5: Commit**

```bash
git add build_app.py tests/test_warm_metal.py
git commit -m "feat: warm the shader cache with 2 threads on Macs under 16 GB (no swap; toolchain tip printed)"
```

---

## Fase 2 — Tier e preset de runtime

### Task 3: `totk_tier.h` / `totk_tier.m` + probe

**Files:**
- Create: `games/totk/third_party/d2r_gfx/gfx/nvn/include/totk_tier.h`
- Create: `games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.m` (compilado automaticamente pelo wildcard `GFX_SRC` do Makefile — sem edição de Makefile para o jogo)
- Create: `games/totk/scripts/tier_probe.c`
- Modify: `games/totk/Makefile` (novo target `probe-tier`, após o bloco do `probe-memory`)

**Interfaces:**
- Produces: `int totk_tier(void)` — 1 = Macs de entrada (família ≤ Apple7 ou RAM < 12 GB), 2 = demais; iOS sempre 2; `TOTK_TIER=1|2` força. `const char *totk_tier_default(const char *env, const char *def_t1, const char *def_t2)` — devolve `env` quando presente (mesmo `"0"`), senão o default do tier (`def_t1` no tier 1, `def_t2` no 2, pode ser `NULL`). Só escolhe o default; o parsing fica com cada knob.
- Consumido pelas Tasks 4-7.

- [ ] **Step 1: Escrever o probe (o teste) e o target do Makefile**

Criar `games/totk/scripts/tier_probe.c`:

```c
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
    printf("{\"tier\":%d,\"default_render\":%s,\"default_mfx\":%s,\"env_wins\":\"%s\",\"ok\":%d}\n",
           t, d_render ? d_render : "null", d_mfx, env, ok);
    return ok ? 0 : 1;
}
```

Em `games/totk/Makefile`, após o bloco `probe-memory` (que termina com `@cat analysis/native-memory-probe.json`), adicionar:

```make
.PHONY: probe-tier
# TOTK_TIER: which Macs get the tier-1 preset (Apple7 GPU family or less than 12 GB RAM); the probe checks the
# totk_tier_default contract too (explicit env wins on every tier; the tier-1 defaults come back only on tier 1).
build/tier-probe: scripts/tier_probe.c third_party/d2r_gfx/gfx/nvn/totk_tier.m third_party/d2r_gfx/gfx/nvn/include/totk_tier.h
	xcrun clang -std=gnu11 -O1 -g -Wall -Wextra -Werror -fobjc-arc -Ithird_party/d2r_gfx/gfx/nvn/include scripts/tier_probe.c third_party/d2r_gfx/gfx/nvn/totk_tier.m -framework Metal -framework Foundation -o "$@"
probe-tier: build/tier-probe
	build/tier-probe > analysis/native-tier-probe.json
	@cat analysis/native-tier-probe.json
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `make probe-tier` (em `games/totk/`)
Expected: erro de compilação — `totk_tier.h: No such file or directory`.

- [ ] **Step 3: Implementar `totk_tier.h` e `totk_tier.m`**

Criar `games/totk/third_party/d2r_gfx/gfx/nvn/include/totk_tier.h`:

```c
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
```

Criar `games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.m`:

```m
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
    dispatch_once(&once, tier_init);
    return g_tier;
}
const char *totk_tier_default(const char *env, const char *def_t1, const char *def_t2) {
    if (env && *env) return env;
    return totk_tier() == 1 ? def_t1 : def_t2;
}
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `make probe-tier` (em `games/totk/`)
Expected: `{"tier":1,"default_render":"1280x720","default_mfx":"1","env_wins":"900","ok":1}` nesta máquina (M1 base 8 GB — a cláusula Apple7 e a de RAM < 12 GB classificam como tier 1). `ok` precisa ser 1.

- [ ] **Step 5: Rodar o resto da suíte e commit**

Run: `python3 -m unittest discover -s tests -v`
Expected: nada quebrado (o probe não afeta os testes Python).

```bash
git add games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.h games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.m games/totk/scripts/tier_probe.c games/totk/Makefile
git commit -m "feat: totk_tier - Apple7/8GB detection with totk_tier_default, probed by make probe-tier"
```

### Task 4: Defaults de runtime no renderer (MetalFX e warm threads) + linha de tier no boot

**Files:**
- Modify: `games/totk/third_party/d2r_gfx/gfx/nvn/totk_mfx_up.m:51-60` (`mfxup_enabled`) e includes do topo
- Modify: `games/totk/third_party/d2r_gfx/gfx/nvn/nvn_mtl.m:28` (include), `:509` (MLOG de tier em `mtl_init`), `:6387-6399` (`warm_threads`)

**Interfaces:**
- Consumes: `totk_tier()` / `totk_tier_default()` da Task 3.

- [ ] **Step 1: Mudar o default do MetalFX (tier 1 → on)**

Em `totk_mfx_up.m`, adicionar o include junto aos demais `#include` do topo (após `#include "totk_mfx_up.h"`): `#include "totk_tier.h"`.

Substituir `mfxup_enabled` (linhas 51-60):

```c
int mfxup_enabled(void) {
    int v = atomic_load_explicit(&g_on, memory_order_relaxed);
    if (v < 0) {
        const char *e = getenv("TOTK_MFX_UPSCALE");
        v = e ? strcmp(e, "0") != 0 : 0;
        atomic_store(&g_on, v);
        ULOG("MetalFX spatial upscaling (TOTK_MFX_UPSCALE): %s", v ? "on" : "off");
    }
    return v;
}
```

por:

```c
int mfxup_enabled(void) {
    int v = atomic_load_explicit(&g_on, memory_order_relaxed);
    if (v < 0) {
        const char *d = totk_tier_default(getenv("TOTK_MFX_UPSCALE"), "1", "0");   // tier 1 preset: MetalFX on by default
        v = strcmp(d, "0") != 0;
        atomic_store(&g_on, v);
        ULOG("MetalFX spatial upscaling (TOTK_MFX_UPSCALE): %s", v ? "on" : "off");
    }
    return v;
}
```

- [ ] **Step 2: Mudar o default dos threads de warm-up (tier 1 → 2)**

Em `nvn_mtl.m`, adicionar `#include "totk_tier.h"` após `#include "totk_sysload.h"` (linha 28).

Em `warm_threads` (linhas 6387-6399), substituir o bloco `dispatch_once` por:

```c
    dispatch_once(&once, ^{
        const char *e = getenv("TOTK_WARM_THREADS");
#if TARGET_OS_IPHONE
        int d = 1;
#else
        int d = (int)NSProcessInfo.processInfo.activeProcessorCount - 2; if (d > 8) d = 8; if (d < 1) d = 1;
        if (totk_tier() == 1 && d > 2) d = 2;   // 8 GB Macs swap when compiling with one thread per core
#endif
        n = e && *e ? atoi(e) : d; if (n < 1) n = 1; if (n > 32) n = 32;
    });
```

- [ ] **Step 3: Linha de observabilidade no boot**

Em `nvn_mtl.m`, `mtl_init`, após a linha 509 (`if (!g_dev.hasUnifiedMemory) MDIE("需要统一内存设备（零拷贝内存池）");`), inserir:

```c
#if !TARGET_OS_IPHONE
    MLOG("GPU tier %d（%s，%llu GB RAM）：tier 1 预设 720p + MetalFX（TOTK_TIER=2 关闭）", totk_tier(),
         g_dev.name.UTF8String ?: "", (unsigned long long)(NSProcessInfo.processInfo.physicalMemory >> 30));
#endif
```

- [ ] **Step 4: Compilar os objetos do renderer (o link completo espera os dados do jogo)**

Run: `make build/gfx/third_party/d2r_gfx/gfx/nvn/totk_mfx_up.m.o build/gfx/third_party/d2r_gfx/gfx/nvn/nvn_mtl.m.o` (em `games/totk/`)
Expected: os dois objetos compilam sem warnings novos (o wildcard `GFX_SRC` já compila o `totk_tier.m` junto no link; o nome do objeto é o caminho + `.o`, então `totk_mfx_up.m.o`).

Nota: o link completo (`make runtime`) depende de `build/runtime_tables.h` etc., gerados da extração do jogo — bloqueado pelo pré-requisito de disco (Global Constraints). A linha de tier no boot é confirmada na medição da Task 9, quando os dados existirem (não bloquear).

- [ ] **Step 5: Commit**

```bash
git add games/totk/third_party/d2r_gfx/gfx/nvn/totk_mfx_up.m games/totk/third_party/d2r_gfx/gfx/nvn/nvn_mtl.m
git commit -m "feat: tier-1 defaults - MetalFX upscaling on and 2 warm-up threads (tier 2 unchanged)"
```

### Task 5: Default 1280x720 para `TOTK_RENDER_RES` (TotK v0/1.1.2/1.2.1 e 1.4.3)

**Files:**
- Modify: `games/totk/runtime/game_patches.c:267-272` (`render_res`) + include no topo
- Modify: `games/totk/runtime/game_patches_v143.c:429-435` (`render_res`) + include no topo

**Interfaces:**
- Consumes: `totk_tier_default()` da Task 3.
- Comportamento: tier 1 sem env → `"1280x720"`; tier 2 sem env → `NULL` → retorno 0 → o jogo usa o próprio tamanho (1600x900 docked), como hoje. Env explícita (`TOTK_RENDER_RES=900` etc.) vence em qualquer tier. iOS (build `CODE_PATCHED_AT_BUILD`): tier 2 → `NULL` → inalterado.

- [ ] **Step 1: `game_patches.c`**

Adicionar `#include "totk_tier.h"` ao bloco de includes locais do topo (após a linha 1, `#include "game_patches.h"`).

Substituir as linhas 267-272:

```c
static int render_res(unsigned *w, unsigned *h) {
    const char *e = getenv("TOTK_RENDER_RES");
    if (!e || !*e || !strcmp(e, "0")) return 0;
```

por:

```c
static int render_res(unsigned *w, unsigned *h) {
    const char *e = totk_tier_default(getenv("TOTK_RENDER_RES"), "1280x720", NULL);   // tier 1 preset: 720p scene, MetalFX upscales
    if (!e || !*e || !strcmp(e, "0")) return 0;
```

(o `sscanf`/`runtime_fail` das linhas seguintes fica igual — o parsing continua neste knob).

- [ ] **Step 2: `game_patches_v143.c`**

Adicionar `#include "totk_tier.h"` ao bloco de includes locais do topo.

Substituir as linhas 429-431:

```c
static int render_res(unsigned *w,unsigned *h){
    const char *e=getenv("TOTK_RENDER_RES");
    if(!e||!*e||!strcmp(e,"0"))return 0;
```

por:

```c
static int render_res(unsigned *w,unsigned *h){
    const char *e=totk_tier_default(getenv("TOTK_RENDER_RES"),"1280x720",NULL);   // tier 1 preset: 720p scene, MetalFX upscales
    if(!e||!*e||!strcmp(e,"0"))return 0;
```

- [ ] **Step 3: Compilar (syntax-only; o link completo espera os dados do jogo)**

Run: `xcrun clang -std=c11 -fsyntax-only -Ithird_party/d2r_gfx/gfx/nvn/include -Iruntime runtime/game_patches.c` e `xcrun clang -std=c11 -fsyntax-only -Ithird_party/d2r_gfx/gfx/nvn/include -Iruntime runtime/game_patches_v143.c` (em `games/totk/`)
Expected: ambos passam sem erros. O header está em `gfx/nvn/include`, já no include path da linha de link do Makefile (`-Ithird_party/d2r_gfx/gfx/nvn/include`). O link completo (`make runtime`) fica para quando a extração existir (pré-requisito de disco).

Risco registrado: o build de iOS compila `runtime/*.c` fora do Makefile (projeto Xcode via `build_app.py`). Se o include path de `gfx/nvn/include` não estiver lá para os arquivos de runtime, o build de iOS falha ao compilar estes dois arquivos — a correção é adicionar o mesmo `-I` ao projeto/target do iOS. Como `totk_tier` no iOS é sempre tier 2, o comportamento do app não muda nem quando o default é calculado no build (`CODE_PATCHED_AT_BUILD`): o default vira `NULL`, exatamente o valor de hoje.

- [ ] **Step 4: Commit**

```bash
git add games/totk/runtime/game_patches.c games/totk/runtime/game_patches_v143.c
git commit -m "feat: tier-1 default 1280x720 scene (TOTK_RENDER_RES still wins; tier 2 keeps the game's own size)"
```

### Task 6: BotW — branch 720p no patch de render

**Files:**
- Modify: `games/totk/runtime/game_patches_botw.c:354-372` (`patch_render_1080`) + include no topo (após a linha 10, `#include "game_version.h"`)

**Interfaces:**
- Consumes: `totk_tier_default()` da Task 3.
- Comportamento: tier 1 sem env → `"720"` → escreve 1280.0 no site; `"900"` → como enviado (1600x900, comportamento de hoje); tier 2 sem env (e envs desconhecidas) → 1920x1080, como hoje. Aceita `"720"` e `"1280x720"` para o 720p (paridade com o knob do TotK).

- [ ] **Step 1: Escrever o código**

Adicionar `#include "totk_tier.h"` após a linha 10 de `game_patches_botw.c`.

Substituir `patch_render_1080` (linhas 354-372):

```c
static void patch_render_1080(Module *m) {
    const char *e = totk_tier_default(getenv("TOTK_RENDER_RES"), "720", NULL);
    if (e && !strcmp(e, "900")) { fprintf(stderr, "[botw] docked render size 1600x900, as shipped (TOTK_RENDER_RES=900)\n"); return; }
    static const struct { uint32_t off, original; } check[] = {
        {0x1821670u, 0xd2a89e08u}, {0x1821674u, 0xf2e89908u}, {0x1821678u, 0xf9094b88u},
    };
    for (size_t i = 0; i < sizeof check / sizeof *check; i++) {
        uint32_t w; memcpy(&w, m->base + check[i].off, 4);
        if (w != check[i].original) {
            fprintf(stderr, "[botw] render size main+%#x: found %08x expected %08x\n", check[i].off, w, check[i].original);
            runtime_fail("render size requires the 1.6.0 display-mode instructions");
        }
    }
    const int w720 = e && (!strcmp(e, "720") || !strcmp(e, "1280x720"));
    const uint32_t movk = w720 ? 0xf2e89408u    /* movk x8, #0x44a0, lsl #48 (1280.0): tier 1 preset */
                               : 0xf2e89e08u;   /* movk x8, #0x44f0, lsl #48 (1920.0) */
    memcpy(m->base + 0x1821674u, &movk, 4);
    fprintf(stderr, "[botw] docked render size %s\n",
            w720 ? "1280x720 (tier 1 preset; TOTK_RENDER_RES=720/1280x720, =900 as shipped)"
                 : "1920x1080 (shipped: 1600x900; TOTK_RENDER_RES=900 keeps it)");
}
```

- [ ] **Step 2: Compilar (syntax-only; o link do binário espera os dados botw160)**

Run: `xcrun clang -std=c11 -fsyntax-only -Ithird_party/d2r_gfx/gfx/nvn/include -Iruntime -DGAME_BOTW=1 -DGAME_BOTW160=1 runtime/game_patches_botw.c` (em `games/totk/`)
Expected: passa sem erros. O link completo (`make build/totk-native-botw160`) depende de `build/botw160/*.h` gerados da extração botw160 (ausente nesta máquina) — registrar o bloqueio no relatório e seguir; a checagem real acontece no próximo build do BotW.

- [ ] **Step 3: Commit**

```bash
git add games/totk/runtime/game_patches_botw.c
git commit -m "feat: BotW docked 720p scene on tier 1 (MetalFX upscales; 900/1080p unchanged)"
```

---

## Fase 3 — Medição e benchmark

### Task 7: `TOTK_BENCH` no `hle_nvn.c`

**Files:**
- Modify: `games/totk/third_party/d2r_gfx/gfx/nvn/hle_nvn.c` — include após a linha 33 (`#include "totk_lifelog.h"`); bloco de benchmark após a linha 2491 (`static int dcmp(...)`), antes de `tsummary`; chamada `bench_collect(frame);` como primeira linha de `tsummary` (linha 2492)

**Interfaces:**
- Produces: quando `TOTK_BENCH=<n>` está ligada (n múltiplo de 300, ≤ 8192), os n frames seguintes à janela de warm-up (frames 301..300+n) são amostrados e uma linha `[bench] {"frame_ms":[...],"gpu_ms":[...],"present_ms":[...],"tier":N}` é impressa no stderr ao final; `TOTK_BENCH_EXIT=1` faz o jogo sair com `_exit(0)` logo após o flush. Off por padrão; env malformada desliga com log.
- Consumido pela Task 8 (`bench_report.py`).

- [ ] **Step 1: Escrever o código (a validação é o run real da Task 9 — não há unit test para o .c do jogo)**

Adicionar `#include "totk_tier.h"` após a linha 33 de `hle_nvn.c`.

Inserir após a linha 2491 (a função `dcmp` que encerra os helpers de timing) e antes de `static void tsummary(uint64_t frame) {`:

```c
// ---------- TOTK_BENCH: per-frame samples for the published M1 benchmarks (docs/m1-benchmarks.md) ----------
// TOTK_BENCH=<n> (n a multiple of TWIN, at most BENCH_MAX): the n frames after the warm-up window (frames 301..300+n)
// are sampled and flushed as one [bench] JSON line at the end; TOTK_BENCH_EXIT=1 quits right after the flush so a
// scripted run measures a fixed scene. The collection reuses the timing windows: at each tsummary the window holds
// exactly the frames since the previous one, so the pushed entries are copied before they are overwritten. GPU spans
// arrive 8 frames late, so gpu_ms has up to a few samples fewer than frame_ms and is not aligned per frame: the report
// uses it for the GPU's p50/p95 over the window, not for per-frame correlation.
#define BENCH_MAX 8192
static double g_bench_iv[BENCH_MAX], g_bench_gpu[BENCH_MAX], g_bench_pres[BENCH_MAX];
static unsigned g_bench_n, g_bench_gpu_n, g_bench_pres_n;
static int g_bench_exit;
static int bench_frames(void) {
    static int n = -1;
    if (n < 0) {
        const char *e = getenv("TOTK_BENCH");
        n = e && *e ? atoi(e) : 0;
        if (n > 0 && n % TWIN) LOG("[bench] TOTK_BENCH must be a multiple of %u (got %d): benchmark off", TWIN, n), n = 0;
        if (n > BENCH_MAX) LOG("[bench] TOTK_BENCH capped at %u", BENCH_MAX), n = BENCH_MAX;
        if (n > 0) { const char *x = getenv("TOTK_BENCH_EXIT"); g_bench_exit = x && strcmp(x, "0") != 0; }
    }
    return n;
}
static void bench_flush(void) {
    fprintf(stderr, "[bench] {\"frame_ms\":[");
    for (unsigned i = 0; i < g_bench_n; i++) fprintf(stderr, "%s%.2f", i ? "," : "", g_bench_iv[i]);
    fprintf(stderr, "],\"gpu_ms\":[");
    for (unsigned i = 0; i < g_bench_gpu_n; i++) fprintf(stderr, "%s%.2f", i ? "," : "", g_bench_gpu[i]);
    fprintf(stderr, "],\"present_ms\":[");
    for (unsigned i = 0; i < g_bench_pres_n; i++) fprintf(stderr, "%s%.2f", i ? "," : "", g_bench_pres[i]);
    fprintf(stderr, "],\"tier\":%d}\n", totk_tier());
    fflush(stderr);
    if (g_bench_exit) _exit(0);
}
static void bench_collect(uint64_t frame) {
    int n = bench_frames(); if (n <= 0) return;
    if (frame <= (uint64_t)TWIN || frame > (uint64_t)(TWIN + n)) return;   // the first window warms up
    static uint64_t p0, p3; unsigned pushed, start;
    pushed = (unsigned)(g_tn[0] - p0); if (pushed > TWIN) pushed = TWIN; start = (unsigned)(p0 % TWIN);
    for (unsigned i = 0; i < pushed && g_bench_n < BENCH_MAX; i++) g_bench_iv[g_bench_n++] = g_tw[0][(start + i) % TWIN];
    pushed = (unsigned)(g_tn[3] - p3); if (pushed > TWIN) pushed = TWIN; start = (unsigned)(p3 % TWIN);
    for (unsigned i = 0; i < pushed && g_bench_gpu_n < BENCH_MAX; i++) { double v = g_tw[3][(start + i) % TWIN]; if (v > 0) g_bench_gpu[g_bench_gpu_n++] = v; }
    for (unsigned i = 0; i < g_presn && g_bench_pres_n < BENCH_MAX; i++) g_bench_pres[g_bench_pres_n++] = g_pres_iv[i];   // before tsummary resets it
    p0 = g_tn[0]; p3 = g_tn[3];
    if (frame == (uint64_t)(TWIN + n)) bench_flush();
}
```

Em `tsummary` (linha 2492), tornar a primeira linha:

```c
static void tsummary(uint64_t frame) {
    bench_collect(frame);
    char line[1024]; size_t o = 0;
```

- [ ] **Step 2: Compilar o objeto**

Run: `make build/gfx/third_party/d2r_gfx/gfx/nvn/hle_nvn.c.o` (em `games/totk/`)
Expected: compila sem warnings novos (o objeto é compilado com `GFX_FLAGS`; `_exit` vem de `<unistd.h>`, já incluído na linha 40 do arquivo; `totk_tier.h` já existe da Task 3). O link completo (`make runtime`) fica para quando a extração existir.

- [ ] **Step 3: Run de sanidade curto (se o jogo boota nesta máquina)**

Run: `python3 scripts/dev/game_slot.py run --no-retry --purpose "bench sanity" -- env TOTK_BENCH=300 TOTK_BENCH_EXIT=1 build/totk-native rewritten/v0 boot > /tmp/bench-sanity.log 2>&1` (em `games/totk/`), depois `grep '^\[bench\]' /tmp/bench-sanity.log`
Expected: uma linha `[bench] {...}` com `frame_ms` de 300 entradas, `"tier":1` e o processo saindo sozinho. Se o jogo não puder rodar aqui (dados não extraídos), pular para a Task 8 — a medição real é a Task 9.

- [ ] **Step 4: Commit**

```bash
git add games/totk/third_party/d2r_gfx/gfx/nvn/hle_nvn.c
git commit -m "feat: TOTK_BENCH samples the frames after warm-up into one JSON line (TOTK_BENCH_EXIT quits)"
```

### Task 8: `bench_report.py` + testes

**Files:**
- Create: `games/totk/scripts/bench_report.py`
- Test: `tests/test_bench_report.py` (novo, raiz do repo — padrão `sys.path.insert` de `test_shader_contrib.py`)

**Interfaces:**
- Consumes: a linha `[bench] {...}` da Task 7 e os marcadores de log (`render resolution|size ...WxH`, `MetalFX spatial upscaling (TOTK_MFX_UPSCALE): on|off`, `GPU tier N`).
- Produces: `python3 games/totk/scripts/bench_report.py <log> [--append docs/m1-benchmarks.md] [--scenario <nome>] [--date YYYY-MM-DD]` — imprime e opcionalmente anexa uma linha de tabela Markdown. Percentis usam a fórmula do próprio jogo (`n*p//100`, cap em `n-1`), para concordar com as linhas `[nvn] 计时`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_bench_report.py`:

```python
"""bench_report.py on synthetic game logs (no game run needed)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'games/totk/scripts'))
import bench_report  # noqa: E402

BENCH = {'frame_ms': [10.0, 20.0, 30.0, 40.0], 'gpu_ms': [8.0, 12.0], 'present_ms': [10.1, -1, 30.0, 16.0], 'tier': 1}


class BenchReportTests(unittest.TestCase):
    def test_last_bench_takes_the_last_line(self):
        text = 'noise\n[bench] {"frame_ms": [], "tier": 2}\n[bench] ' + json.dumps(BENCH) + '\n'
        self.assertEqual(bench_report.last_bench(text), BENCH)
        self.assertIsNone(bench_report.last_bench('nothing here\n'))

    def test_metrics_match_the_games_own_ranks(self):
        m = bench_report.metrics(BENCH)
        self.assertEqual(m['frames'], 4)
        self.assertEqual(m['p50'], 30.0)       # n*50//100 = 2 -> 3rd of sorted [10,20,30,40]
        self.assertEqual(m['p95'], 40.0)       # n*95//100 = 3 -> capped at n-1
        self.assertEqual(m['p99'], 40.0)
        self.assertEqual(m['fps'], 40.0)       # 1000 / mean(10,20,30,40)
        self.assertEqual(m['over16_7_pct'], 75.0)
        self.assertEqual(m['drops'], 2)        # -1 (discarded) and 30.0 (> 25 ms)
        self.assertEqual(m['gpu_p50'], 12.0)
        self.assertEqual(m['gpu_p95'], 12.0)

    def test_config_reads_the_game_log_markers(self):
        text = ('[dm] [nvn-mtl] GPU tier 1（Apple M1）\n'
                '[totk] 1.4.3 render resolution: 1280x720 on a 1920-wide canvas\n'
                '[dm] [nvn-mtl] [mfx-up] MetalFX spatial upscaling (TOTK_MFX_UPSCALE): on\n')
        self.assertEqual(bench_report.config(text), ('1280x720', 'on'))
        self.assertEqual(bench_report.config('nothing\n'), ('', ''))

    def test_append_writes_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / 'bench.md'
            md.write_text('| date |\n|---|---|\n')
            log = Path(tmp) / 'log.txt'
            log.write_text('[botw] docked render size 1280x720\n[bench] ' + json.dumps(BENCH) + '\n')
            r = subprocess.run([sys.executable, str(ROOT / 'games/totk/scripts/bench_report.py'), str(log),
                                '--append', str(md), '--scenario', 'great plateau', '--date', '2026-10-20'],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('| 2026-10-20 | great plateau | tier 1 | 1280x720 |', md.read_text().splitlines()[-1])

    def test_no_bench_line_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'log.txt'
            log.write_text('nothing\n')
            r = subprocess.run([sys.executable, str(ROOT / 'games/totk/scripts/bench_report.py'), str(log)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn('no [bench] line', r.stderr)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `python3 -m unittest discover -s tests -p test_bench_report.py -v`
Expected: `ModuleNotFoundError: No module named 'bench_report'`

- [ ] **Step 3: Implementar**

Criar `games/totk/scripts/bench_report.py`:

```python
#!/usr/bin/env python3
"""TOTK_BENCH report: the [bench] JSON line of a game log -> metrics, printed and/or appended to docs/m1-benchmarks.md.

The game (hle_nvn.c) flushes one [bench] line per TOTK_BENCH run; the last one in the log wins (scripted runs may
append). Percentiles use the game's own rank (n*p//100, capped at n-1) so the table agrees with the [nvn] summary lines.
"""
import argparse
import json
import re
import statistics
import sys
from datetime import date


def last_bench(text):
    """The last [bench] {...} JSON object of a log, or None."""
    found = [line[len('[bench] '):] for line in text.splitlines() if line.startswith('[bench] ')]
    return json.loads(found[-1]) if found else None


def pct(v, p):
    """Nearest-rank percentile, the game's own formula (hle_nvn.c tsummary)."""
    s = sorted(v)
    if not s:
        return None
    i = len(s) * p // 100
    return s[min(i, len(s) - 1)]


def config(text):
    """(render size, MetalFX flag) from the game's own log markers; '' where the game did not log them."""
    render = mfx = ''
    m = re.search(r'render (?:resolution|size)[^\n]*?(\d{3,4}x\d{3,4})', text)
    if m:
        render = m.group(1)
    m = re.search(r'MetalFX spatial upscaling \(TOTK_MFX_UPSCALE\): (on|off)', text)
    if m:
        mfx = m.group(1)
    return render, mfx


def metrics(bench):
    frame = bench['frame_ms']
    pres = bench['present_ms']
    drops = sum(1 for x in pres if x < 0 or x > 25.0)
    over = sum(1 for x in frame if x > 16.7)
    avg = statistics.mean(frame) if frame else 0.0
    return {
        'frames': len(frame),
        'avg_ms': avg,
        'fps': 1000.0 / avg if avg else 0.0,
        'p50': pct(frame, 50), 'p95': pct(frame, 95), 'p99': pct(frame, 99),
        'over16_7_pct': 100.0 * over / len(frame) if frame else 0.0,
        'drops': drops,
        'gpu_n': len(bench['gpu_ms']),
        'gpu_p50': pct(bench['gpu_ms'], 50), 'gpu_p95': pct(bench['gpu_ms'], 95),
        'tier': bench['tier'],
    }


def fmt(v):
    return '-' if v is None else f'{v:.1f}'


def row(bench, text, scenario, day):
    render, mfx = config(text)
    m = metrics(bench)
    return (f"| {day} | {scenario} | tier {m['tier']} | {render or '-'} | {mfx or '-'} | "
            f"{m['frames']} | {m['fps']:.1f} | {fmt(m['p50'])} | {fmt(m['p95'])} | {fmt(m['p99'])} | "
            f"{m['over16_7_pct']:.1f}% | {m['drops']} | {fmt(m['gpu_p50'])} | {fmt(m['gpu_p95'])} |")


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('log', help='the game log with the [bench] line')
    p.add_argument('--append', metavar='MD', help='append the row to this Markdown table file (docs/m1-benchmarks.md)')
    p.add_argument('--scenario', default='camp', help='what the run measured (table column)')
    p.add_argument('--date', default=None, help='run date YYYY-MM-DD (default: today)')
    a = p.parse_args(argv)
    with open(a.log, encoding='utf-8', errors='replace') as f:
        text = f.read()
    bench = last_bench(text)
    if bench is None:
        p.error(f'{a.log}: no [bench] line (run the game with TOTK_BENCH=<n>)')
    r = row(bench, text, a.scenario, a.date or date.today().isoformat())
    print(r)
    if a.append:
        with open(a.append, 'a', encoding='utf-8') as f:
            f.write(r + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Rodar e confirmar que passam**

Run: `python3 -m unittest discover -s tests -p test_bench_report.py -v`
Expected: 5 testes passam.

- [ ] **Step 5: Commit**

```bash
git add games/totk/scripts/bench_report.py tests/test_bench_report.py
git commit -m "feat: bench_report.py turns a TOTK_BENCH log into a markdown table row (unit-tested)"
```

### Task 9: `docs/m1-benchmarks.md` + a medição real (capstone)

**Files:**
- Create: `docs/m1-benchmarks.md`
- (rows preenchidos pelo run real desta máquina)

**Gate:** esta task roda só quando a extração existir (`extracted/v0`, `rewritten/v0`, `build/*.h` de config) — hoje bloqueada pelo pré-requisito de disco das Global Constraints (havia ~12 GB livres contra os ~45–60 GB necessários). Com os dados presentes, o jogo pode ser usado já construído ou com `make runtime`. Nada das Tasks 1–8 depende desta task.

- [ ] **Step 1: Escrever o esqueleto do documento**

Criar `docs/m1-benchmarks.md`:

```markdown
# M1 benchmarks (tier 1)

Numbers on the M1 base (8 GPU cores, 8 GB RAM) — the machine the tier-1 preset targets. Every row comes from a real run:
`TOTK_BENCH=<frames> TOTK_BENCH_EXIT=1` with the log redirected, then `bench_report.py --append`. M4 rows (tier 2) are the
no-regression reference. The preset: 1280x720 scene upscaled to the 1920x1080 canvas with MetalFX.

## How to run

```bash
cd games/totk
make runtime
python3 scripts/dev/game_slot.py run --no-retry --purpose "bench" -- \
  env TOTK_BENCH=900 TOTK_BENCH_EXIT=1 build/totk-native rewritten/v0 boot > /tmp/bench.log 2>&1
python3 scripts/bench_report.py /tmp/bench.log --append ../../docs/m1-benchmarks.md --scenario "great plateau camp"
```

(`TOTK_TIER=2` repeats a run with today's defaults; a specific knob, e.g. `TOTK_RENDER_RES=900`, overrides just that knob.)

## Build — shader step (step_warm_metal)

| date | Mac | threads | time | swap? | toolchain? |
|---|---|---|---|---|---|
| (baseline) 2026-10-18 | M1 base 8 GB | 6 (cores − 2) | ~25 min | heavy | no |
| 2026-10-20 | M1 base 8 GB | 2 | ? | ? | ? |

## Frame rate (docked, 60 Hz)

| date | scenario | tier | render | mfx | frames | avg fps | p50 | p95 | p99 | %>16.7 | drops | gpu p50 | gpu p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 | great plateau camp | tier 1 | 1280x720 | on | 900 | ? | ? | ? | ? | ? | ? | ? | ? |
```

- [ ] **Step 2: Rodar a medição tier 1 e preencher a linha**

Run (em `games/totk/`): `make runtime` e depois `python3 scripts/dev/game_slot.py run --no-retry --purpose "bench tier1" -- env TOTK_BENCH=900 TOTK_BENCH_EXIT=1 build/totk-native rewritten/v0 boot > /tmp/bench-tier1.log 2>&1`
Expected: o jogo abre, roda 1200 frames (300 de warm-up + 900 medidos) e sai sozinho; `grep '^\[bench\]' /tmp/bench-tier1.log` mostra a linha JSON com `"tier":1`.

Run: `python3 scripts/bench_report.py /tmp/bench-tier1.log --append ../../docs/m1-benchmarks.md --scenario "great plateau camp"` e substituir os `?` da linha nova pelos valores impressos.

- [ ] **Step 3: Rodar o controle tier 2 (não-regressão) e preencher a linha**

Run: `python3 scripts/dev/game_slot.py run --no-retry --purpose "bench tier2" -- env TOTK_TIER=2 TOTK_BENCH=900 TOTK_BENCH_EXIT=1 build/totk-native rewritten/v0 boot > /tmp/bench-tier2.log 2>&1`
Expected: linha JSON com `"tier":2`; `grep 'render resolution' /tmp/bench-tier2.log` não mostra 1280x720 (o jogo usa o próprio tamanho) e `MetalFX spatial upscaling (TOTK_MFX_UPSCALE): off`.

Run: `python3 scripts/bench_report.py /tmp/bench-tier2.log --append ../../docs/m1-benchmarks.md --scenario "great plateau camp"` e preencher a linha.

- [ ] **Step 4: Comparar contra os critérios da spec e registrar**

Critérios (spec, seção 1): 60 FPS em 1080p (coluna `avg fps` ≥ 60 na linha tier 1, com a imagem conferida visualmente no run — o upscale MetalFX aparece na janela 1920x1080); etapa de shaders sem swap (a linha do build na seção de build: tempo bem abaixo de ~25 min e sem swap — conferir `memory_pressure`/tempo no log do build); tier 2 sem regressão (linha tier 2 comparável aos números M4/anteriores, e a Task 4 já garantiu defaults idênticos). Se 60 FPS não fechar, a spec aponta os ajustes (fator de upscale, palco do MetalFX, TAA opcional) — abrir issue com os números em vez de ajustar às cegas.

- [ ] **Step 5: Commit**

```bash
git add docs/m1-benchmarks.md
git commit -m "docs: first M1 benchmarks - tier-1 720p+MetalFX vs tier-2 control (great plateau camp)"
```

---

## Verificação final do plano inteiro

Verificáveis agora (Tasks 1–8):

- [ ] `python3 -m unittest discover -s tests -v` — suíte Python inteira verde (Tasks 1, 2, 8).
- [ ] `make probe-tier` — `"ok":1` e `"tier":1` (Task 3).
- [ ] Objetos do renderer compilam: `make build/gfx/third_party/d2r_gfx/gfx/nvn/totk_mfx_up.m.o build/gfx/third_party/d2r_gfx/gfx/nvn/nvn_mtl.m.o build/gfx/third_party/d2r_gfx/gfx/nvn/hle_nvn.c.o` (Tasks 4, 7).
- [ ] Syntax-only dos arquivos de runtime: `game_patches.c`, `game_patches_v143.c` e `game_patches_botw.c` (Tasks 5, 6) com os comandos das tasks.

Gated na extração (pré-requisito de disco, Task 9):

- [ ] `make runtime` e `make build/totk-native-botw160` linkam sem warnings.
- [ ] `/tmp/bench-tier1.log` tem `[bench]` com `"tier":1` e `/tmp/bench-tier2.log` com `"tier":2` (Tasks 7, 9).
- [ ] `docs/m1-benchmarks.md` tem as linhas medidas, não `?` (Task 9).

Sempre:

- [ ] `git log --oneline` mostra os commits das tasks em ordem (1-2, 3, 4, 5, 6, 7, 8), sem commits de trabalho intermediário esquecidos.
