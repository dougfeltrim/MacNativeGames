# Design: melhorias para o M1 (preset por tier)

- Data: 2026-10-20
- Status: aprovado em brainstorming (seções 1–4 revisadas e aprovadas); aguardando revisão final
- Escopo: build e primeiro launch, runtime (FPS/gráficos), medição e benchmark
- Hardware de validação: MacBook com M1 base (GPU de 8 núcleos, 8 GB de RAM)

## 1. Objetivo e contexto

O projeto suporta qualquer Mac Apple silicon com macOS 26+ (Metal 4), mas o único dado de M1 documentado é negativo: a etapa de shaders do build leva cerca de 25 minutos num M1 de 8 GB, que faz swap durante a compilação ([build_app.py](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/build_app.py), `step_warm_metal`). Os testes de FPS publicados são em M4 (≈90 FPS após warm-up); não há benchmark de M1.

Metas:

1. **60 FPS na saída 1080p via MetalFX** no M1 base em modo docked, com a cena interna renderizada em **1280x720** e o MetalFX fazendo o upscale para o canvas 1920x1080 (padrão semelhante ao já usado no iPhone: cena abaixo do canvas).
2. Etapa de shaders do build **sem swap pesado** no M1 de 8 GB (abaixo de ~15 min, ou substituída pelo caminho do Metal toolchain).
3. Números de M1 **medidos e publicados** em `docs/m1-benchmarks.md`.
4. **Nenhuma regressão** em M4/iOS: o preset só altera defaults em máquinas de tier baixo, e env vars explícitas sempre vencem.

## 2. Decisões e não-objetivos

- Abordagem escolhida: **preset automático por tier** (alternativas discutidas no brainstorming: só documentar knobs — rejeitada por exigir ação do usuário; motor adaptativo em tempo real — rejeitada por escopo/risco, YAGNI).
- **Fora de escopo**: resolução dinâmica adaptativa em tempo real, `TOTK_MFX_OUTPUT` ligado por padrão, TAA (tecla 5) por padrão, streaming sensível a pressão de memória, suporte a macOS 15 (fork comunitário Metal 3).
- **iOS não muda**: os defaults de handheld são próprios e continuam como estão.

## 3. Componente novo: detecção de tier (`totk_tier`)

Arquivos novos: `games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.h` e `totk_tier.m`. O renderer é compartilhado entre TotK e BotW (`games/botw` não tem renderer próprio), então o preset vale para os dois jogos.

- **Entradas**: maior família de GPU suportada (`supportsFamily`, Apple7→Apple10) e `hw.memsize` via `sysctlbyname` (leitura que já existe em `nvn_mtl.m`).
- **Classificação**: tier 1 = família máxima Apple7 (M1/A14) **ou** RAM < 12 GB; tier 2 = demais. A cláusula de RAM cobre propositalmente Macs de 8 GB mais novos (ex.: M2 Air).
- **API**: `int totk_tier(void)` — lazy init com `dispatch_once` (padrão dos knobs atuais) — e `const char *totk_tier_default(const char *env, const char *def_t1, const char *def_t2)` — devolve a string da env se presente, senão a do tier. A função só **escolhe o default**; o parsing continua com cada knob, como hoje (`atoi` para threads/resolução, `strcmp` para flags).
- **Falha segura**: família de GPU não identificável e RAM desconhecida → defaults legados (comportamento atual). `TARGET_OS_IPHONE` → sempre legado.
- **Override de teste**: `TOTK_TIER=1|2` força o tier, para A/B sem trocar de hardware.
- **Observabilidade**: `MLOG` de startup informa tier, RAM, família de GPU e defaults efetivos.
- **Consumidores**: `totk_mfx_up.m` (default de `TOTK_MFX_UPSCALE`) e `nvn_mtl.m` (default de `TOTK_WARM_THREADS`). Aniso não muda: o default 0 atual é o correto para o tier 1.

## 4. Build e primeiro launch

Problema: `step_warm_metal` roda `warm_metal_cache` com threads = cores − 2 (6 no M1); cada compilação do serviço Metal aloca bastante memória e o M1 de 8 GB entra em swap (~25 min). A ferramenta já aceita um argumento de threads.

- **Threads conscientes de RAM** (`build_app.py`): ler `hw.memsize`; abaixo de 16 GB, invocar `warm_metal_cache` com `threads=2` e logar o motivo; 16 GB+ mantém o comportamento atual.
- **Recomendação ativa do Metal toolchain**: quando `step_shader_libs` não está disponível (toolchain não instalado) e a máquina tem < 16 GB, o build instrui `xcodebuild -downloadComponent MetalToolchain` — esse caminho compila os shaders dentro do app uma única vez, funciona onde o app for movido e evita o swap por completo. Continua opcional.
- **Warm-up do jogo**: default de `TOTK_WARM_THREADS` passa por `totk_tier_default` — tier 1 → 2 threads (hoje: cores − 2, máx. 8). O env override continua valendo.
- **Princípio preservado**: qualquer falha cai no caminho atual (o jogo compila os shaders no primeiro start); nenhuma dessas mudanças pode quebrar o build.
- **Teste**: rebuild no M1 de 8 GB medindo tempo e swap (`memory_pressure` no log do build), verificação de hits com `--verify`, comparação com a linha de base documentada (~25 min).

## 5. Runtime (preset do tier 1)

Em docked, o jogo renderiza a cena abaixo do canvas e faz o próprio upscale (estilo FSR) para o canvas 1920x1080; `mfxup_dock`/`mfxup_draw` substituem esse upscale pelo MetalFX quando `TOTK_MFX_UPSCALE` está ligado — hoje **off** por padrão (`totk_mfx_up.m`, `mfxup_enabled`).

- **Preset tier 1 (Mac, docked)**: `TOTK_RENDER_RES` default **1280x720** + `TOTK_MFX_UPSCALE` default **on** (upscale MetalFX 720p → canvas 1080p). Fator de upscale maior que o do iPhone → mais folga de GPU para segurar 60 FPS, imagem um pouco mais suave.
- **Passo de verificação na implementação**: confirmar que `TOTK_RENDER_RES` governa a cena docked do TotK (o README documenta para o BotW). Se não governar, o preset usa o mecanismo que governa, mantendo 1280x720 como alvo. O palco do MetalFX (`MFXUP_SCENE` linear-HDR antes do tone mapping, ou `MFXUP_DOCKED` pós-FXAA) é o que a medição da fase 3 mostrar melhor; o código de `mfxup_draw` já cobre o caso "cena menor que o canvas".
- **O que não muda**: aniso segue 0; orçamento de GPU segue 40% da RAM (~3,2 GB no M1 de 8 GB — suficiente); `TOTK_MFX_OUTPUT` (janelas acima de 1080p) continua off; tier 2+ intacto.
- **Desligar o preset**: `TOTK_TIER=2` restaura o comportamento de hoje; envs explícitas individuais (`TOTK_MFX_UPSCALE=0`, `TOTK_RENDER_RES=...`) sempre vencem o default.
- **Fallback automático**: MetalFX indisponível no dispositivo (`g_unavailable`, tratamento existente) → upscale próprio do jogo; o preset nunca quebra a imagem.
- **BotW**: mesmo preset via renderer compartilhado. O BotW docked atual renderiza 1080p nativo, então o ganho no M1 é maior ainda; os números do BotW entram na medição quando ela acontecer (suporte ainda early).
- **Teste**: A/B no M1 base (720p+MetalFX vs 900p nativo vs 1080p nativo) em cenas densas e mundo aberto, com os contadores existentes (16,7 ms, p50/p95/p99, drops a cada 300 frames) e checagem visual do upscale; M4 como controle de não-regressão.

## 6. Medição e benchmark

- **`TOTK_BENCH=<frames>`**: opt-in; amostra os N frames seguintes após o load com câmera parada e imprime um resumo JSON no log reusando a infraestrutura existente (`g_tw`, `tsummary` de 300 frames): p50/p95/p99 do intervalo, % acima de 16,7 ms, drops (>25 ms ou descartados), stalls de compilação de shader e memória. Off por padrão; env malformada é ignorada.
- **`games/totk/scripts/bench_report.py`**: transforma as linhas JSON do `totk.log` numa tabela Markdown (cenário × preset × métricas), com teste de fixture mínimo.
- **`docs/m1-benchmarks.md`**: números antes/depois no M1 base de 8 GB — tempo e swap da etapa de shaders do build, warm-up do primeiro launch, FPS por cena e preset — no estilo dos docs de teste existentes, mais uma linha M4 de referência (tier 2).
- **Critérios de sucesso que a medição decide**: 60 FPS na saída 1080p em cenas representativas do M1 base; etapa de shaders do build sem swap pesado; nenhuma regressão M4/iOS. Se o preset não segurar 60 FPS, a medição orienta o ajuste (fator de upscale, palco do MetalFX, ou TAA opcional).

## 7. Riscos e mitigação

- `TOTK_RENDER_RES` pode não governar a cena docked do TotK → verificação antecipada na implementação; fallback para o mecanismo que governa; alvo 1280x720 mantido.
- Qualidade visual do upscale 720p→1080p pode desagradar → A/B visual documentado no benchmark; envs para voltar ao padrão; TAA (tecla 5) medido como opção.
- Swap residual no M1 de 8 GB mesmo com 2 threads → caminho do Metal toolchain é a solução primária; fatiamento com pausa do `warm_metal_cache` fica como próximo passo se necessário.
- `supportsFamily` pode se comportar diferente em Metal 4/macOS 26 → fallback por RAM classifica o M1 de 8 GB corretamente de qualquer forma (cláusula RAM < 12 GB).
- Regressão M4/iOS → mudanças são tier-gated, envs vencem, e a validação cruzada no M4 é item do plano.

## 8. Fases de implementação

1. **Build e primeiro launch** (`build_app.py` + default de warm threads no jogo) — independente, ataca o gargalo documentado.
2. **Tier + preset de runtime** (`totk_tier` + defaults 720p/MetalFX) — depende do item 1 apenas por compartilhar o helper de tier (que pode nascer na fase 1).
3. **Medição** (`TOTK_BENCH` + `bench_report.py` + `docs/m1-benchmarks.md`) — valida as fases 1 e 2 e decide os ajustes finais do preset.

## 9. Fatos de código que sustentam o design

- `build_app.py` `step_warm_metal` (≈linhas 984–1034): threads padrão, mensagem do M1/M4, fallback; `step_shader_libs` (linha 1000) marca o caminho do Metal toolchain.
- `games/totk/tools/warm_metal_cache.m`: aceita `[threads]` (linha 129); padrão cores − 2, máx. 8.
- `totk_mfx_up.m` `mfxup_enabled` (linhas 51–55): `TOTK_MFX_UPSCALE`, default off.
- `nvn_mtl.m`: `mfxup_dock` (≈9634), `mfxup_draw` (≈9705, caso "cena menor que o canvas"); `warm_threads` (6387–6399, `TOTK_WARM_THREADS`); orçamento de GPU por `hw.memsize` (290–293).
- `hle_nvn.c` (2500–2504): baseline M1 de 16,7 ms nos contadores de frame.
- `games/botw/` não possui `d2r_gfx` próprio: usa o renderer compartilhado em `games/totk/third_party/d2r_gfx`.
