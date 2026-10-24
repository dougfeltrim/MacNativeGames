# Design: FSR 4.1.1 como backend de upscale, sem Xcode e acoplável

- Data: 2026-10-22
- Status: aprovado em brainstorming (seções 1–5 revisadas e aprovadas uma a uma); autorevisão concluída; aguardando revisão do arquivo pelo mantenedor
- Escopo: um kit reutilizável em `vendor/fsr4kit/`, um seam de backend de upscale na camada gráfica, e a série de incrementos que decide se isso vira feature
- Branch desta série: `feat/fsr41`, aberta em `main` (3649704) sem commits próprios ainda
- Hardware de validação: MacBook M1 base, GPU de 7 núcleos (família Apple7), 8 GB de RAM, macOS 27.0.1, **apenas Command Line Tools**

## 1. Objetivo e contexto

[`fsr4-native-macos`](https://github.com/UzenUPoZiTiV4ik/fsr4-native-macos) (v1.9, 2026-10-21) porta o AMD FSR 4.1.1 para Apple silicon via Metal e hoje é distribuído como ferramenta de linha de comando e como hook dentro do CrossOver (DLSS → FSR 4). O objetivo aqui é colocar o mesmo upscaler dentro do renderer nativo deste projeto, como backend selecionável ao lado do MetalFX.

O encaixe existe e é bom. O [`hook/fsr4_metalfx.m`](https://github.com/UzenUPoZiTiV4ik/fsr4-native-macos/blob/main/hook/fsr4_metalfx.m) do upstream intercepta a criação de `MTLFXTemporalScaler` e troca o `encodeToCommandBuffer:` por `fsr4_encode_live_frame_v2()`. Nós somos donos do código: no [`totk_taa.m`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/games/totk/third_party/d2r_gfx/gfx/nvn/totk_taa.m) já existe um scaler temporal recebendo color + depth + motion vectors por frame, então chamamos a função direto, sem swizzle.

Três fatos sustentam a linha de trabalho:

1. **Não há backend de upscale enumerado hoje.** `MFXUP_SCENE`/`MFXUP_DOCKED`/`MFXUP_OUTPUT` são contextos de resolução, não backends. Introduzir um upscaler novo sem um seam significa ou encaixá-lo à força num módulo que assume MetalFX, ou duplicar caminhos ad hoc.
2. **O FSR4 é temporal e depende de motion vectors; os nossos são sintetizados.** `totk_taa.m:9-11` declara "What moves on own (Link, enemies) gets the camera's motion". Isso é um risco de qualidade real e não está medido em lugar nenhum.
3. **O upstream não tem nenhum número de M1.** `docs/HARDWARE.md` manda Apple7 para o backend `simd` e quatro documentos diferentes afirmam que M1–M4 não foram medidos. Toda a decisão de viabilidade é, hoje, especulação.

Por isso esta série é **gateada**: duas medições decidem antes de qualquer feature ser escrita.

**Metas**

1. Rodar o FSR 4.1.1 nesta máquina **sem Xcode e sem Metal Toolchain**, porque o projeto inteiro depende de compilar Metal em runtime.
2. Entregar um módulo **acoplável por outros apps** (as outras portas de jogo e qualquer app Metal), com API C e sem dependência de python ou swizzle em runtime.
3. **Nenhum impacto no iPhone** nem nos defaults atuais: FSR4 desligado por padrão nos dois tiers.

**Não-objetivos**

- Perfil `lossy2` e seus pesos treinados: são GPL-2.0-or-later no upstream e não são vendorados; CONTRIBUTING.md proíbe código GPL neste repo.
- Paridade com o caminho CrossOver (hook `libfsr4hook.dylib` x86_64 sob Rosetta): superfície duplicada, fora desta série.
- Backend `i8mpp` (tensor nativo): exige família 10+, não existe no M1.
- FSR4 rodando em mais de um contexto de upscale ao mesmo tempo (o status do upstream é por processo; ver §6).
- Reescrever os estágios `pre`/`post`/`pad` como MSL nosso. A dívida do fork R3 (§3) foi aceita explicitamente no brainstorming em vez disso.

## 2. Decisões de arquitetura

Abordagem escolhida no brainstorming: **seam primeiro, FSR4 depois** (alternativas rejeitadas: encaixe direto dentro de `totk_mfx_up.m` — mistura dois ABI e dois ciclos de vida num módulo que hoje funciona; estágio temporal único substituindo TAA e o scaler espacial juntos — é o destino arquitetural correto e fica como incremento futuro em cima do seam, não como primeiro passo).

**A rota de compilação escolhida é R3, com R2 descartada.** As três rotas que existiam:

| Rota | Como | Veredito |
|---|---|---|
| R1 | instalar Xcode 26 + Metal Toolchain | Rejeitada: contradiz como o projeto funciona hoje (`docs/m1-benchmarks.md` registra "só Command Line Tools") |
| R2 | cache pré-compilado num job de CI com Xcode, artefato versionado no molde de `games/totk/prebuilt/macos-arm64/totk_xlate` | Rejeitada como caminho principal: pinaria os artefatos a uma versão de toolchain e poria CI no caminho crítico de qualquer mudança de shader |
| **R3** | **fontes `.metal` geradas carregadas em runtime por `newLibraryWithSource`** | **Escolhida**: elimina a dependência de toolchain offline e é exatamente o padrão do app (`totk_taa.m:116`, `nvn_mtl.m:4955,9570`). Os artefatos do bundle viram fonte legível e auditável |

O ponto que R3 resolve e que parecia bloquear tudo: `tools/compile_fsr411.py:398-400` invoca `xcrun metal` e `xcrun metallib`, e `fsr411_runtime.h:933` carrega o `.metallib` por `newLibraryWithURL`. Os estágios que o backend `simd` não cobre vêm daí. O fork R3 troca essa carga pela fonte que `compile_fsr411.py:353` já escreve ao lado do `.air`.

**Ordem da série com gate numérico pré-acordado**: medição de performance antes de qualidade, qualidade antes de refactor. O refactor do seam (§4) só é escrito se os dois gates passarem.

## 3. Camada 1 — o kit (`vendor/fsr4kit/`)

Módulo autônomo, no molde de `vendor/ryujinx/`. A dependência corre em um sentido só: o app conhece o kit; o kit não conhece o app.

**API pública** (`include/fsr4kit.h`). Decisão de desenho: **não reinventar a struct de frame**. `Fsr4LiveFrameV2` (`src/fsr4_live.h:37-45`) já traz `structSize` + `abiVersion` e usa `void*` para device, command buffer, fence e texturas — portanto é chamável de C, C++ ou Swift sem ponte. O kit só adiciona o que o upstream resolve com `getenv`, que é a interface errada para uma biblioteca:

```c
typedef struct {
    const char *data_root;        /* src/ e gen/ abertos por caminho em runtime */
    int profile;                  /* 0 exact | 1 lossy | 2 exact-fast */
    int backend;                  /* 0 auto | 1 simd | 2 scalar */
    float sharpness; int rcas;
    int allow_live_compile;       /* 0 no kit: nunca invocar python */
    int stub_motion_policy;       /* era FSR4_STUB */
} Fsr4KitConfig;

int  fsr4kit_init(const Fsr4KitConfig *);
int  fsr4kit_encode(const Fsr4LiveFrameV2 *);      /* delega a fsr4_encode_live_frame_v2 */
void fsr4kit_invalidate(void *device, void *owner);
void fsr4kit_release(void *device, void *owner);
int  fsr4kit_status(Fsr4LiveStatus *out);
const char *fsr4kit_reason(void);
```

Os knobs de ambiente do upstream (`FSR4_COMPILE`, `FSR4_BUILD_DIR`, `FSR4_ROOT`, `FSR4_MODEL`, `FSR4_PROFILE`, `FSR4_SHARPNESS`, `FSR4_RCAS`, `FSR4_SETTINGS`, `FSR411_SIMD_WINOGRAD`, `FSR4_STUB`) viram campos do config, lidos do env **só** quando `data_root == NULL`, para não quebrar o `fsr4mac` da medição. `fsr4_panel_request()` — chamado em `fsr4_live.mm:1490`, que lança `dist/crossover/FSR4 Panel.app` — sai da compilação do kit por flag de build.

**Subconjunto vendorado.** Verificado: ~304 KB em 16 arquivos (`fsr4_live.mm`, `fsr411_live.mm`, `fsr411_runtime.h`, `fsr411_model.h`, `fsr4_input.h`, `fsr411_capture_io.h`, `fsr411_scratch_layout.h`, `fsr4_backend.h`, `fsr4_device.h`, `fsr4_log.h`, `fsr411_simd.metal`, `fsr411_helpers.metal`, `fsr411_reference.metal`, `pass0_weights.h`, `i8mpp_host.h`, `fsr4_panel.mm`). Três notas sobre a lista: `i8mpp_host.h` entra só como dependência de link de `fsr4_live.mm:22-24` e nunca é usado no caminho `simd`; `fsr411_reference.metal` entra como fallback de paridade (é o `--scalar`/`--reference` que o gate de teste compara); `fsr4_panel.mm` entra só pelo acesso a `Fsr4LiveStatus`, sem o launcher de `FSR4 Panel.app` (§3 API). Ficam de fora `fsr4mac.mm` (77 KB), `fsr411_cli.mm`, `hook/`, e `fsr411_mpp.metal`/`fnb_mpp.metal`/`i8mpp.metal` (família 10+). Payload não-fonte que o `data_root` precisa conter: `third_party/fsr4` (11 MB), `third_party/fsr411` (2,4 MB, com `initializers.bin` de 131.072 B), mais o `gen/` gerado — cujo tamanho **o incremento 1 mede**, não esta spec.

**Layout**

```
vendor/fsr4kit/
  include/fsr4kit.h
  src/                subset vendorado + o patch R3 no carregador
  tools/              compile_fsr411.py, gen_fsr411_native.py — passo de mantenedor
  data/src/           os .metal que o runtime abre por caminho (root_+"/src/...")
  data/gen/           fsr411_native_1080_lossy_simd/, bindings.json, *_weights.bin, *_params.bin
  data/third_party/   fsr4 + fsr411, para regenerar
  Makefile            clang++ -> libfsr4kit.a   (nenhum xcrun metal)
  LICENSE.AMD.md  NOTICE.upstream.md  README.vendored.md
```

**O patch R3, em três mudanças.** `compile_fsr411.py:398-400` deixa de invocar `xcrun metal`/`metallib` mantendo o `.metal` que `:353` escreve; a chave `library` do `bindings.json` passa a `source`; `fsr411_runtime.h:933-934` troca `newLibraryWithURL` por `newLibraryWithSource` com `MTLLanguageVersion2_3` — igual ao que `:672` já faz para os helpers. A meia mudança restante é `compile_fsr411.py:165`, que mistura `xcrun metal --version` no digest do cache: no kit a chave passa a ser o hash das próprias fontes geradas. O codegen python vira passo de **mantenedor**, rodado uma vez por bump de versão do upstream, com saída commitada; em runtime o kit não invoca python nem toolchain alguma.

## 4. Camada 2 — o seam (`totk_upscale`)

Arquivo novo em `games/totk/third_party/d2r_gfx/gfx/nvn/`. Mac-only, excluído de `games/totk/ios/project.yml`.

```objc
typedef enum { UPSCALE_METALFX = 0, UPSCALE_FSR4 = 1, UPSCALE_N } upscale_backend;
typedef struct {
    id<MTLTexture> color;                    /* cena, in_w x in_h */
    id<MTLTexture> depth, motion;            /* nil => backend espacial */
    NSUInteger in_w, in_h, out_w, out_h;
    double jitter_x, jitter_y;               /* pixels; 0,0 sem jitter */
    int hdr, depth_reversed, reset;
} upscale_frame;
id<MTLTexture> upscale_encode(int ctx, id<MTL4CommandBuffer> cb, const upscale_frame *f);
void upscale_set(int ctx, upscale_backend b);
id<MTLResidencySet> upscale_residency(int ctx);
void upscale_stats(int ctx, char *out, size_t n);
```

**Regra de isolamento**: `totk_upscale_fsr4.m` é o único arquivo da camada gráfica que inclui `fsr4kit.h`. A dependência de ML nunca vaza para `nvn_mtl.m` nem para os outros títulos.

**Pontos de migração** já mapeados: `mfxup_draw()` em `nvn_mtl.m:10393-10434` (hook por draw), `mfxup_dock()` em `nvn_mtl.m:10329-10386`, estágio de output em `nvn_mtl.m:12332-12349`, residency em `nvn_mtl.m:10314-10316`, flags `mfx_upscale`/`mfx_output` em `nvn_mtl.m:2389,2394`. O cache de 3 contextos, a tabela `bad[]` de retry e o fence (`totk_mfx_up.m:26,33-41`) continuam **dentro** do backend MetalFX, não sobem para a interface. `upscale_encode` devolvendo nil mantém o idioma dos dois módulos: o caminho do caller permanece.

## 5. Fluxo de dados por frame

```
G-buffer pass (Depth32Float, tamanho do colour, >=3 anexos)   [nvn_mtl.m:4390-4397]
  ├─ bloco de câmera, 84 floats do uniform 1 do vertex         [nvn_mtl.m:9865-9878]
  ├─ jitter Halton(2,3)/16 na geometria                        [totk_taa.m:231-232]
  └─ depth latchada para o frame                               [mfx_note_depth]
                     │
taa_frame_end ─ jit_ok┤  (jitter só vale depois de um frame aceito)  [totk_taa.m:228-230]
                     │
compose draw 0ac9a549 ┴─► upscale_encode(ctx=DOCKED, {color, depth, motion, jitter, reset})
                    ┌────┴─────────────┐
             UPSCALE_METALFX      UPSCALE_FSR4
             scaler espacial      taa_mv compute → MV
             1280x720→1920x1080   fsr4kit_encode(Fsr4LiveFrameV2), owner = token do backend
```

**Reaproveitamento direto.** O kernel `taa_mv` (`totk_taa.m:101-114`) reprojeta a depth do frame com `prevViewProj × inverse(curViewProj)` e é despachado com fence no mesmo command buffer antes do encode (`:208-213`). Os campos do frame FSR4 saem do que hoje vai ao scaler MetalFX: `mvScaleX = mvScaleY = 1`, `depthReversed = NO`, `reset` (`totk_taa.m:217`), `jitterX/Y = g.jx/g.jy` em pixels centrados no texel com `jit_used` decidindo entre o valor real e zero (`:216`). **`jitteredMV = 0`** é o valor correto: a reprojeção é puramente de câmera e não embute o jitter, que segue em campo separado — a convenção que `docs/FSR411_INPUT_CONTRACT.md:15` pede. O contrato confirma que as propriedades públicas do MetalFX já têm o mesmo sinal do FSR ("no additional sign flip or exposure multiplication", `hook/fsr4_metalfx.m`), então **não** se adiciona flip de DLSS.

**Identidade do histórico.** O upstream amarra histórico e estado recorrente ao objeto scaler (`src/fsr4_live.h:16-19`), porque o D3DMetal cria mais de um por frame. Sem objeto scaler, o backend cunha um token estável por `(ctx, device)` que vira `owner`; `fsr4kit_release` no teardown, `fsr4kit_invalidate` quando o próprio seam pular um frame.

**Geometria.** `fsr4_live.mm:1492-1498` valida o `Fsr4InputGeometry` contra os tamanhos reais das texturas e recusa se divergir; o seam preenche `width/height/offsetX/offsetY` do `MTLTexture`, não do tamanho lógico pedido. O caso docked é feliz nos alinhamentos: 1280, 720, 1920 e 1080 são todos múltiplos de 8, o alinhamento interno do FSR4. O contexto scene (960×540 em handheld) **não** é — 540/8 = 67,5 — e é também por isso que o FSR4 entra só no docked.

**Dois nós, ambos decididos por medição e não por argumento:**

- **F1 vs F2 (onde a cor entra).** F1: entrada é o canvas tone-mapped na resolução da cena, no lugar exato do scaler espacial. Drop-in, e como tone mapping não move geometria, depth e MV continuam válidos. Contra: a rede recebe imagem pós-tone-map, sem exposure HDR. F2: entrada é a cena linear HDR (como o TAA consome hoje) e o tone map move para depois do upscale — casa com `preExposure` e auto-exposure, mas reordena o draw `0ac9a549`. O incremento 3 despeja **os dois pontos** e roda as duas variantes.
- **TAA e FSR4 não coexistem.** Dois estágios temporais em série não é configuração definida no upstream, e o FSR4 assume uma amostra jittered por frame — o TAA 1:1 entrega exatamente o contrário. Selecionar FSR4 no docked implica TAA desligado, com o motivo escrito no código.

## 6. Erro, fallback e warm-up

O princípio vem do upstream: *"until the encoder returns success the original encode still runs, so a bad build does not black the frame."* Três camadas com escopos diferentes:

| Camada | Exemplos | Reação | Escopo |
|---|---|---|---|
| **Init (permanente)** | `FSR4_ROOT is required` (`fsr411_live.mm:128`), `native 4.1.1 ABI mismatch` (`fsr411_runtime.h:736`), `missing native 4.1.1 weights/parameters`, falha de compilação MSL (`:780`), `unsupported Metal compute encoder` (`:369`), `Metal 4 command buffer lacks residency API` (`:1048`) | backend morto, `fsr4kit_reason()` logado **uma vez**, contexto pinado em `UPSCALE_METALFX` pela sessão | sem retry: com `allow_live_compile=0` nada muda em runtime, logo é erro de empacotamento pego por teste |
| **Frame (transiente)** | `caller command buffer already has an active encoder` (`:364`), `cannot allocate FSR 4.1.1 textures or scratch` (`:764`), rejeição de geometria | devolve 0 → **MetalFX roda este frame** + `fsr4kit_invalidate`; N falhas consecutivas pinam pelo backoff que já existe (`MFXUP_RETRY_MS = 10000`, `totk_mfx_up.m:26`) | por frame, no idioma do app |
| **Conteúdo (gracioso)** | sem depth ou câmera no frame — menus, loading, cortes | placeholder de motion 4×4 RGBA8 que o kit reconhece (`fsr411_live.mm:157-176`): copia o output FSR4 anterior, invalida histórico, retorna 1. Não cai para MetalFX | caminho normal do loop, não exceção |

A camada de conteúdo é a descoberta mais útil do levantamento: o CrossOver enfrenta jogos sem motion vectors e a resposta desenhada é o stub, que casa com a regra do perfil Lossy de atualizar cor sem MV (`docs/STATE.md:14-19`). Como nossos MV são sintetizados de câmera, isso é parte normal do funcionamento. Hoje, no mesmo caso, `taa_encode` devolve nil (`totk_taa.m:196`).

**Invariantes espelhadas.** Todo caminho de erro do upstream chama `owner->invalidate()` e limpa `lastOutput`/`lastOwner` (`fsr411_live.mm:145-150`) — nunca sobra histórico órfão. O ciclo de vida de texturas substituídas é tratado dentro do kit (`context->collect()`, o `retire`/`FrameSlot::kDone`), pelo mesmo motivo do `MFXUP_OLD_AFTER` do app: command buffer MTL4 não retém recursos.

**Resolução de um risco que parecia grande.** O kit opera com `__bridge id` do command buffer (`fsr411_live.mm:168,178`) e este app codifica em Metal 4. Não há incompatibilidade: o runtime **sonda** o nível do encoder — `m4 = ![enc respondsToSelector:@selector(setBuffer:offset:atIndex:)]` (`fsr411_runtime.h:288,365`) — e tem caminho MTL4 completo, com `MTL4ArgumentTable`, `setArgumentTable:` e `bind_residency` via `useResidencySet:` (`:1048`). Só `options.stageProfiler` é rejeitado em MTL4 (`:367`), e é caminho de CLI.

**Duas consequências de código que viram restrição de design.** (a) O status do upstream é **por processo** (`fsr4_live.h:80-88`), sem chave por contexto; com três contextos, dois FSR4 simultâneos mostrariam o motivo errado no HUD — reforça docked-only, e a linha de stats tem de nomear o contexto. (b) **Warm-up é requisito, não otimização**: ~27 estágios gerados mais os kernels nativos compilados por `newLibraryWithSource` na primeira encode real é um stall de segundos num M1. O backend precisa de estado `warm`: inicialização e compilação numa background queue no boot, e o seam mantém MetalFX até o backend reportar pronto. A classe de custo é a mesma de `docs/first-launch-shaders.md`.

**Reset.** Reusa os três gatilhos que já existem (`!prev_ok`, buraco de frame, `camera_cut()` em `totk_taa.m:186-190`) mais os do FSR4: mudança de tamanho lógico de output, de perfil e de backend.

## 7. Empacotamento, licença e platform gate

- **`build_app.py` ganha uma etapa** que monta `data/` do kit no bundle e registra fingerprints e hashes nos checks existentes, como §"record what it used and produced" de CONTRIBUTING.md pede — não um mecanismo novo.
- **`FSR4_ROOT` é obrigatório** (`fsr411_live.mm:128`): o `data_root` tem de ser caminho de filesystem no bundle, não blob em memória.
- **Licença.** Não existe arquivo LICENSE na raiz do upstream, embora `README.md:79` o chame de MIT. As concessões reais: `third_party/fsr411/LICENSE` (MIT, AMD 2025 + int3rrobang 2026), `LICENSE.AMD.md` (exceção MIT do SDK 2.3.0), `NOTICE.upstream.md`, `third_party/fsr4/README.upstream.md` (FidelityFX SDK 2.0.0 MIT @`01446e6`). Cada licença fica ao lado do código, e o `THIRD_PARTY_NOTICES.md` ganha linha nova. Os pesos do perfil `lossy2` são GPL-2.0-or-later e **não entram**.
- **iOS.** `run()`/python/`NSTask` não existem na plataforma. Com `allow_live_compile=0` o kit nunca tenta shell-out, então é Mac-only por construção, além de excluído do `project.yml` do iOS.
- **Assinatura.** O app já passa por `codesign --deep` de um bundle de ~16 GB; o tamanho extra de `data/gen/` é medido no incremento 1 antes de qualquer decisão sobre o que entrar no bundle.

## 8. Testes, validação e medição

**O gate usa a máquina que já existe.** CONTRIBUTING manda usar as ferramentas existentes, não criar timer. `TOTK_BENCH` amostra frames fixos numa linha JSON e `bench_report.py` imprime/anexa a linha (`feat/bench`, `docs/m1-benchmarks.md`). O critério do incremento 2 fica executável: tier 1, save `slot_04`, 30 FPS, `TOTK_FSR4=0` e `=1`, GPU p50 contra a baseline medida de **10,02 ms**, e projeção de duty pela fórmula da tabela de calor (`GPU p50 × fps`). A barra é **≤ ~18 ms/frame**, o ponto em que o duty a 30 FPS chegaria a 0,85 s/s — o patamar que `docs/changes/2026-10-21-heat-30fps.md` já trata como problemático num M1 sem ventoinha.

| Teste | O que afirma | Onde roda |
|---|---|---|
| `ci: fsr4-metallib-vs-source` | saída do build oficial (`xcrun metal`) e do kit (fonte em runtime) **byte-idênticas** no caso `1280 720 1920 1080 8` | CI com Xcode |
| subset portável do `make test` do upstream (`backend_policy`, `input_contract`, `fsr411_math`, `scratch_layout`, `settings`) | política de backend e ABI de entrada intactos após o fork | local, só CPU |
| `test-fsr411-simd` (paridade simd ↔ scalar byte-idêntica, `HARDWARE.md:26-28`) | o carregador de fonte não quebra a garantia de exatidão do backend | local, GPU |
| `tests/test_fsr4_kit_build.py` | o `Makefile` do kit não menciona `xcrun metal`/`metallib`; hashes de `data/gen/` batem com o manifest | local |
| `tests/test_fsr4_defaults.py` | matriz tier × env: FSR4 desligado em tier 1 **e** 2; `TOTK_FSR4` sempre vence, espelhando `totk_tier_default` (`totk_tier.m:29-32`) | local, probe com `xcrun clang` no molde de `tests/test_tier_fps60.py:15-40` |
| `tests/test_fsr4_fallback.py` | encode 0 → frame via MetalFX **e** `fsr4kit_invalidate`; init quebrado → pinado permanente | local, probe |
| `tests/test_upscale_seam.py` | incremento 4 não muda comportamento: mesma sequência de chamadas, mesma saída MetalFX | local |
| `tests/test_fsr4_frames.py` | contrato do dump (color/depth/mv/jitter/câmera) — o harness do incremento 3 e o runtime do 6 falam o mesmo formato | local |
| `tests/test_fsr4_ios_excluded.py` | `games/totk/ios/project.yml` não referencia `fsr4kit`; `build_app.py --platform ios` continua construindo | local |

**Efeito colateral desejado do R3.** Hoje `make test-fsr411-gpu` exige Metal Toolchain justamente pelos metallibs. Se o patch cobrir o caminho, as suites de GPU do upstream — inclusive a paridade simd/scalar, que é a garantia de exatidão inteira — passam a rodar nesta máquina. A dívida do fork é também o que destrava o teste.

**Baseline própria, porque não há golden publicado.** `testcases/`, `build/`, `dist/` são gitignored e os goldens DX12 (`golden-v0.1`) não estão no clone. O incremento 1 captura a própria saída determinística e a versiona como fixture de regressão (hashes + previews PNG). Isso prova determinismo e auto-consistência; a equivalência com o build oficial é o job de CI acima que prova, e a spec não finge que a comparação local cobre isso.

**Nada regride.** Os ~271 testes da série M1 continuam verdes; defaults do tier 2 não mudam; o draw de upscale próprio do jogo (`a3a570eb`, FSR1-style) continua sendo o caminho do caller quando `upscale_encode` devolve nil.

**Como se testa em máquina, por plataforma** (CONTRIBUTING): jogar no Mac, nomeando a versão do jogo e o ponto do jogo. Teste verde não substitui jogar.

## 9. Série de incrementos

Branches empilhadas na ordem, um doc em `docs/changes/` por incremento, restack sobre `main` a cada merge.

| # | Branch | Entrega | Gate |
|---|---|---|---|
| 1 | `spike/fsr4-xcode-free` | provar a premissa: patch R3, codegen offline só com CLT + `dxc`/`spirv-cross` do upstream, `fsr4mac --simd --profile lossy` rodando no caso `1280 720 1920 1080 8` | se não rodar sem Xcode, a linha morre aqui com ~0 código no app. Mede também: tamanho de `data/gen/`, quais estágios compilam em runtime, custo da primeira encode |
| 2 | `spike/fsr4-m1-perf` | ms/frame por perfil (lossy, exact-fast, exact), pico de memória nos 8 GB, projeção de duty a 30 FPS | **barra de 18 ms/frame no FSR4**, derivada assim: baseline medida de 10,02 ms + 18 ms = 28,02 ms × 30 fps = 841 ms de GPU por segundo = duty 0,84 s/s, o limiar de calor de `docs/changes/2026-10-21-heat-30fps.md` |
| 3 | `spike/fsr4-real-frames` | dump off-by-default de color+depth+MV+jitter no encode do `totk_taa.m`, replay offline, A/B contra o MetalFX atual em Link, guardians e inimigos; variantes F1 e F2 | veredito de ghosting com MV de câmera-only contra "no loss of picture quality" |
| 4 | `refactor/upscale-seam` | extrair `totk_upscale`, só MetalFX, **comportamento zero alterado**; builds macOS e iOS | só existe se 2 e 3 passarem |
| 5 | `feat/fsr4kit-vendor` | o kit em `vendor/fsr4kit/`: Makefile, `data/`, API C, testes de paridade, licenças + linha no `THIRD_PARTY_NOTICES.md` | `make test` do kit verde sem Xcode |
| 6 | `feat/fsr4-backend` | backend FSR4 no seam: depth/MV chegando ao estágio de upscale, acoplamento de jitter, dono do histórico, estado `warm`, fallback | |
| 7 | `feat/fsr4-optin` | `TOTK_FSR4`, tecla, linha de HUD, README, `docs/changes/` | desligado por padrão nos dois tiers |

**Escopo do plano de implementação.** Este design especifica a série inteira porque as decisões (§2 a §8) só fazem sentido juntas, mas o plano que sai do `writing-plans` cobre **só o incremento 1**. Os incrementos 2 e 3 são medições cujo desenho depende do que o 1 descobrir (quais estágios carregam, qual o custo real da primeira encode), e os incrementos 4 a 7 são **condicionais por construção**: se o gate 2 ou o gate 3 reprovar, eles não são reescritos — são apagados, e o artefato final da série passa a ser o doc de reprovação com as tabelas medidas. Isso é intencional: o trabalho caro desta linha é medir, não integrar.

## 10. Riscos abertos

| Risco | Estado |
|---|---|
| Desempenho de `simd` no M1 nunca foi medido por ninguém, incluindo o upstream | é o gate 2. Estimativa honesta: o M5 faz 4,88–6,13 ms com `simd` em 864×560→1712×1112; nosso caso tem ~1,9× mais pixels de entrada e o half-matrix do M1 é bem mais lento → faixa provável 20–40 ms, ou seja, o gate pode reprovar |
| MV de câmera-only causa ghosting em objetos que se movem sozinhos | o upstream não documenta comportamento com MV aproximados; `FSR411_GHOSTING_FIX.md` trata de um NaN na diferença Cb/Cr do histórico, medido com MV reais. É o gate 3 |
| Estágios gerados podem exigir linguagem acima de 2.3 em compilação runtime (`compile_fsr411.py:398` usa `-std=macos-metal2.3`; o caminho nativo usa 3.0 em `fsr411_runtime.h:778`) | se um estágio específico falhar, marcá-lo incompatível e manter o A/B só com os perfis que carregarem. Não fingir que funciona |
| Dívida do fork R3: cada release do upstream (ritmo semanal, v1.9 em 2026-10-21) exige re-aplicar e re-validar o patch | aceita explicitamente no brainstorming. O job `ci: fsr4-metallib-vs-source` é o que transforma a regravação em verificação mecânica |
| Memória em 8 GB nunca foi medida sob pressão pelo upstream (`FSR411_MEMORY_OPTIMIZATION.md:53-55` declines; os dados são M5/16 GB) | arena de 20.880.256 B no tier 1080 + estado persistente (~40 MB em 1920×1080) cabe com folga na conta; o gate 2 mede o pico real e a falha já é graciosa por §6 |

## 11. Fatos de código que sustentam o design

- [`totk_taa.m:151`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/games/totk/third_party/d2r_gfx/gfx/nvn/totk_taa.m) — `d.inputWidth = d.outputWidth = src.width`: o scaler temporal hoje é **1:1**, sem upscale.
- `totk_taa.m:101-114,199-213` — kernel `taa_mv`, reprojeção `prevViewProj × inverse(curViewProj)`, dispatch com fence antes do encode.
- `totk_taa.m:214-217` — o que vai ao scaler: color, depth, motion, output, `jitterOffsetX/Y`, `motionVectorScale = 1`, `depthReversed = NO`, `reset`.
- `totk_taa.m:9-11` — MV vêm só da câmera. `totk_taa.m:186-190` — `camera_cut()`. `totk_taa.m:228-232` — Halton(2,3)/16 e a regra de jitter só depois de um frame aceito.
- [`totk_mfx_up.m:7-12`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/games/totk/third_party/d2r_gfx/gfx/nvn/totk_mfx_up.m) — `MFXUP_DOCKED` (Mac): só o scaler **espacial**; os três contextos e as resoluções de cada um. `:26,33-41` — cache de 3, tabela `bad[]`, `MFXUP_RETRY_MS`.
- `nvn_mtl.m:4390-4397` — heurística do G-buffer; `nvn_mtl.m:9865-9878` — captura das 84 floats de câmera; `nvn_mtl.m:10329-10386,10393-10434,12332-12349` — os três pontos de upscale; `nvn_mtl.m:2389,2394` — flags `mfx_upscale`/`mfx_output`.
- [`totk_tier.m:13-36`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/games/totk/third_party/d2r_gfx/gfx/nvn/totk_tier.m) — tier 1 = Apple7 ou <12 GB; `totk_tier_default(env, def_t1, def_t2)`; `totk_fps60()`.
- [`build_app.py`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/build_app.py) `step_shader_libs` (~972), `metal_compiler` (~927), `step_warm_metal` (~1388) — o precedente de compilar Metal e o fallback quando a toolchain não existe.
- [`docs/m1-benchmarks.md`](/Users/douglasfeltrim/Documents/GitHub/MacNativeGames/docs/m1-benchmarks.md) — tier 1, 1280×720 + MetalFX → 1920×1080: GPU p50 10,17 ms a 60 FPS (duty ~0,85 s/s) e 10,02 ms a 30 FPS (duty ~0,30 s/s). A máquina descrita no doc é "só Command Line Tools".
- upstream [`src/fsr4_live.h:16-19,37-45,80-88`](https://github.com/UzenUPoZiTiV4ik/fsr4-native-macos/blob/main/src/fsr4_live.h) — dono do histórico, ABI v2, status por processo.
- upstream `src/fsr411_runtime.h:672-673` (helpers, 2.3), `:769-780` (simd nativo em runtime, 3.0), `:733,869` (shell-out para python), `:736,764,940` (falhas), `:933-934` (carga do metallib), `:288,365` (sonda `m4`), `:1048` (residency MTL4).
- upstream `src/fsr411_live.mm:128` (`FSR4_ROOT` obrigatório), `:145-150` (`owner->invalidate()` em toda falha), `:157-176` (stub de motion 4×4), `:178` (encode).
- upstream `tools/compile_fsr411.py:165` (digest com `xcrun metal --version`), `:353` (escreve o `.metal`), `:398-400` (`xcrun metal` + `metallib`).
- upstream `src/fsr411_scratch_layout.h:13-16` — arena por tier: 1080 → 20.880.256 B, 2160 → 83.232.256 B, 4320 → 332.352.256 B.
- upstream `docs/HARDWARE.md` — M1/família 7 → `simd`; zero números de M1 no repositório.
