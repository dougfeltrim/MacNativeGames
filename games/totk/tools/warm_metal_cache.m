// warm_metal_cache: compile the app's startup shaders once at packaging time, so the player's first start finds them in
// the system's Metal cache instead of compiling them in the background for about three minutes (M4, 8 threads; one
// thread took 14 minutes).
//
// Metal keeps the compiled libraries of programs without an app bundle (the game binary is one: the launcher starts it)
// in one store, keyed by the shader source text, the compile options and the length of the path of the folder the
// program runs from -- only its length (2026-10-18): a program in a folder whose path is one character longer finds
// nothing, while any program in a folder with a path as long (same folder, other name or code-signing identifier,
// another volume, another depth) finds everything; the working directory and the way the program was started do not
// matter. So build_app.py copies this tool next to the game binary (Contents/Resources/totk/build), runs it there and
// removes it again. The game compiles each <variant>_b.metal of its shader cache with exactly these options
// (nvn_mtl.m totk_precise_opts / totk_msl_library), and the startup seed is copied into that cache byte for byte
// (totk_shader_seed.h), so compiling the seed's files here gives the game cache hits (0.03-0.5 ms instead of 50-130 ms).
// The store is shared by all such programs, holds about 1.08 GB and evicts heavily when one of them compiles many new
// libraries, and loses what a program compiled while another program that opened it first was running as soon as that
// program quits. So this runs last, only while no other program has the store open (build_app.py metal_cache_users), is
// checked with --verify in a new process, and takes a budget (--budget) so it does not push out the entries it made.
//
// The seed has byte-identical files (2,071 of 22,643 in the 1.4.3 seed): a file whose text was already compiled in this
// run is not compiled again. build_app.py also passes an ordered list (--list): the shaders of the title list first (the
// game only waits for those before the first frame), then the game's manifest, then the seed's manifest, then the rest,
// with the largest file first inside each group (the expensive ones do not become a long tail at the end).
//
// usage: warm_metal_cache <folder with *_b.metal> [threads] [--list <file>] [--budget <n>]
//        warm_metal_cache <folder> --verify <count> [--list <file>]
//        --budget 0 (the default) compiles everything; a compile that takes 5 ms or more counts against it, and a hit
//        (the store already had it) does not. --verify reads its sample from the folder, or, with --list, the first
//        <count> files of the list.
// prints one JSON line: files, unique, duplicates, missing, compiled, cold, hits, skipped, failed, threads, seconds,
// compile_seconds_summed, budget_stopped, remaining, throttled_seconds.
// build: xcrun clang -fobjc-arc -O2 tools/warm_metal_cache.m -framework Metal -framework Foundation -o build/warm-metal-cache
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <CommonCrypto/CommonDigest.h>
#include <dispatch/dispatch.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

// The game's options (nvn_mtl.m totk_precise_opts): exactly rounded IEEE float arithmetic. Anything else is another
// cache entry. (The game also has a fastMathEnabled = NO branch for macOS before 15; the app needs macOS 26.)
static MTLCompileOptions *game_options(void) {
    MTLCompileOptions *o = [MTLCompileOptions new];
    o.mathMode = MTLMathModeSafe;
    return o;
}

// The game's warm-up skips these (nvn_mtl.m warm_shader_load): Metal has no double, and spirv-cross marks what it could
// not translate.
static int skipped_source(NSString *src) {
    return [src containsString:@"double "] || [src containsString:@"unsupported_GLSLstd450"];
}

static NSArray<NSString *> *shader_sources(NSString *dir) {
    NSError *e = nil;
    NSArray<NSString *> *names = [NSFileManager.defaultManager contentsOfDirectoryAtPath:dir error:&e];
    if (!names) { fprintf(stderr, "warm_metal_cache: cannot list %s: %s\n", dir.UTF8String, e.localizedDescription.UTF8String); exit(2); }
    NSMutableArray<NSString *> *out = [NSMutableArray array];
    for (NSString *n in [names sortedArrayUsingSelector:@selector(compare:)])
        if ([n hasSuffix:@"_b.metal"]) [out addObject:[dir stringByAppendingPathComponent:n]];
    return out;
}

// The order build_app.py wrote: one absolute path per line, blank lines and # comments ignored.
static NSArray<NSString *> *listed_sources(NSString *path) {
    NSError *e = nil;
    NSString *text = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:&e];
    if (!text) { fprintf(stderr, "warm_metal_cache: cannot read the list %s: %s\n", path.UTF8String, e.localizedDescription.UTF8String); exit(2); }
    NSMutableArray<NSString *> *out = [NSMutableArray array];
    for (NSString *line in [text componentsSeparatedByString:@"\n"]) {
        NSString *p = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (p.length && ![p hasPrefix:@"#"]) [out addObject:p];
    }
    return out;
}

// The text as the file holds it (the game hashes the bytes of the .metal it compiles): a second file with the same text
// is the same cache entry. Streaming, so a 1 MB shader does not become a 1 MB temporary string.
static NSString *file_digest(NSString *path) {
    int fd = open(path.fileSystemRepresentation, O_RDONLY);
    if (fd < 0) return nil;
    CC_SHA256_CTX ctx; CC_SHA256_Init(&ctx);
    uint8_t buf[1 << 16]; ssize_t n;
    while ((n = read(fd, buf, sizeof buf)) > 0) CC_SHA256_Update(&ctx, buf, (CC_LONG)n);
    close(fd);
    if (n < 0) return nil;
    unsigned char out[CC_SHA256_DIGEST_LENGTH]; CC_SHA256_Final(out, &ctx);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", out[i]];
    return hex;
}

// One library as the game compiles it: the file's UTF-8 text as is. Returns the compile time in ms, -1 when skipped,
// -2 when it does not compile. A compile cut off by the system's compiler service being stopped is retried, as in the
// game (totk_msl_library).
static double compile_one(id<MTLDevice> dev, MTLCompileOptions *opts, NSString *path, NSString **why) {
    NSString *src = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
    if (!src) { if (why) *why = @"cannot read the file"; return -2; }
    if (skipped_source(src)) return -1;
    for (int attempt = 1;; attempt++) {
        NSError *e = nil; CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
        id<MTLLibrary> lib = [dev newLibraryWithSource:src options:opts error:&e];
        double ms = (CFAbsoluteTimeGetCurrent() - t0) * 1e3;
        if (lib) return ms;
        BOOL interrupted = [e.localizedDescription containsString:@"XPC_ERROR_CONNECTION_INTERRUPTED"];
        if (!interrupted || attempt == 4) { if (why) *why = e.localizedDescription ?: @"no error text"; return -2; }
        usleep((useconds_t)attempt * 100000);
    }
}

// Hits take 0.1-0.5 ms, a library Metal has not seen 50-160 ms on an M1; 5 ms separates them.
enum { HIT_MS = 5 };

// Parallelism that backs off when the Mac runs out of memory: 8 GB Macs swap with one compile per core, and another
// program (the game, a browser) may be using the cores too. The workers wait here instead of piling up in the Metal
// compiler service.
static NSCondition *gate;
static long active, limit;
static _Atomic long long throttled_us = 0;

static void gate_init(long threads) { gate = [NSCondition new]; limit = threads; }

static void gate_set_limit(long n) {
    if (n < 1) n = 1;
    [gate lock]; limit = n; [gate broadcast]; [gate unlock];
}

static void gate_acquire(void) {
    [gate lock];
    CFAbsoluteTime t0 = 0; BOOL waited = NO;
    while (active >= limit) { if (!waited) { t0 = CFAbsoluteTimeGetCurrent(); waited = YES; } [gate wait]; }
    active++;
    [gate unlock];
    if (waited) atomic_fetch_add(&throttled_us, (long long)((CFAbsoluteTimeGetCurrent() - t0) * 1e6));
}

static void gate_release(void) { [gate lock]; active--; [gate broadcast]; [gate unlock]; }

static dispatch_source_t memory_pressure_source(int threads) {
    dispatch_source_t src = dispatch_source_create(DISPATCH_SOURCE_TYPE_MEMORYPRESSURE, 0,
                                                   DISPATCH_MEMORYPRESSURE_WARN | DISPATCH_MEMORYPRESSURE_CRITICAL | DISPATCH_MEMORYPRESSURE_NORMAL,
                                                   dispatch_queue_create("warm_metal_cache.memory", DISPATCH_QUEUE_SERIAL));
    dispatch_source_set_event_handler(src, ^{
        unsigned long level = dispatch_source_get_data(src);
        if (level & DISPATCH_MEMORYPRESSURE_CRITICAL) gate_set_limit(1);
        else if (level & DISPATCH_MEMORYPRESSURE_WARN) gate_set_limit(threads / 2);
        else gate_set_limit(threads);
    });
    dispatch_resume(src);
    return src;
}

static int compile_all(id<MTLDevice> dev, NSArray<NSString *> *all, int threads, long budget) {
    NSMutableArray<NSString *> *files = [NSMutableArray array];
    NSMutableSet<NSString *> *seen = [NSMutableSet set];
    long duplicates = 0, missing = 0;
    for (NSString *path in all) {
        NSString *digest = file_digest(path);
        if (!digest) { missing++; continue; }
        if ([seen containsObject:digest]) { duplicates++; continue; }
        [seen addObject:digest];
        [files addObject:path];
    }
    if (duplicates || missing)
        fprintf(stderr, "warm_metal_cache: %ld files are byte-identical to one already in the list, %ld cannot be read\n",
                duplicates, missing);

    MTLCompileOptions *opts = game_options();
    gate_init(threads);
    dispatch_source_t pressure = memory_pressure_source(threads);
    __block _Atomic long next = 0, compiled = 0, cold = 0, hits = 0, skipped = 0, failed = 0;
    __block _Atomic int stopped = 0;
    __block _Atomic long long busy_us = 0;
    NSMutableArray<NSString *> *problems = [NSMutableArray array];
    CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
    dispatch_group_t g = dispatch_group_create();
    dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
    for (int t = 0; t < threads; t++) dispatch_group_async(g, q, ^{
        for (;;) {
            if (budget > 0 && atomic_load(&cold) >= budget) { atomic_store(&stopped, 1); break; }
            long i = atomic_fetch_add(&next, 1);
            if (i >= (long)files.count) break;
            @autoreleasepool {
                gate_acquire();
                NSString *why = nil; double ms = compile_one(dev, opts, files[(NSUInteger)i], &why);
                gate_release();
                if (ms == -1) atomic_fetch_add(&skipped, 1);
                else if (ms == -2) {
                    atomic_fetch_add(&failed, 1);
                    @synchronized (problems) { if (problems.count < 5) [problems addObject:[NSString stringWithFormat:@"%@: %@", files[(NSUInteger)i].lastPathComponent, why]]; }
                } else {
                    atomic_fetch_add(&compiled, 1);
                    atomic_fetch_add(&busy_us, (long long)(ms * 1e3));
                    if (ms >= HIT_MS) atomic_fetch_add(&cold, 1); else atomic_fetch_add(&hits, 1);
                }
                long done = atomic_load(&compiled) + atomic_load(&skipped) + atomic_load(&failed);
                if (done % 1000 == 0) fprintf(stderr, "warm_metal_cache: %ld / %lu after %.0f s\n", done, (unsigned long)files.count, CFAbsoluteTimeGetCurrent() - t0);
            }
        }
    });
    dispatch_group_wait(g, DISPATCH_TIME_FOREVER);
    dispatch_source_cancel(pressure);
    for (NSString *p in problems) fprintf(stderr, "warm_metal_cache: does not compile: %s\n", p.UTF8String);
    long done = atomic_load(&compiled) + atomic_load(&skipped) + atomic_load(&failed);
    printf("{\"files\":%lu,\"unique\":%lu,\"duplicates\":%ld,\"missing\":%ld,\"compiled\":%ld,\"cold\":%ld,\"hits\":%ld,"
           "\"skipped\":%ld,\"failed\":%ld,\"threads\":%d,\"seconds\":%.1f,\"compile_seconds_summed\":%.1f,"
           "\"budget_stopped\":%s,\"remaining\":%ld,\"throttled_seconds\":%.1f}\n",
           (unsigned long)all.count, (unsigned long)files.count, duplicates, missing, atomic_load(&compiled),
           atomic_load(&cold), atomic_load(&hits), atomic_load(&skipped), atomic_load(&failed), threads,
           CFAbsoluteTimeGetCurrent() - t0, atomic_load(&busy_us) / 1e6, atomic_load(&stopped) ? "true" : "false",
           (long)files.count - done, atomic_load(&throttled_us) / 1e6);
    return 0;
}

static int cmp_double(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }

// Without --list the sample is spread over the folder; with it, the list's first <count> files (build_app.py puts the
// title-screen shaders there, and asks whether those are in the store already).
static int verify(id<MTLDevice> dev, NSArray<NSString *> *files, int count, BOOL spread) {
    MTLCompileOptions *opts = game_options();
    // The first request of a process also starts the connection to the compiler service: not part of the sample.
    [dev newLibraryWithSource:@"#include <metal_stdlib>\nkernel void warm_metal_cache_hello(uint i [[thread_position_in_grid]]) {}\n" options:opts error:nil];
    if (count > (int)files.count) count = (int)files.count;
    double *ms = calloc((size_t)(count > 0 ? count : 1), sizeof *ms); int n = 0, hits = 0;
    for (int k = 0; k < count && files.count; k++) @autoreleasepool {
        NSUInteger i = spread ? (NSUInteger)((double)k * (double)files.count / count) : (NSUInteger)k;
        double t = compile_one(dev, opts, files[i], NULL);
        if (t < 0) continue;
        ms[n++] = t; if (t < HIT_MS) hits++;
    }
    qsort(ms, (size_t)n, sizeof *ms, cmp_double);
    printf("{\"sampled\":%d,\"hits\":%d,\"median_ms\":%.2f,\"max_ms\":%.2f}\n", n, hits, n ? ms[n / 2] : 0.0, n ? ms[n - 1] : 0.0);
    free(ms);
    return 0;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 2) { fprintf(stderr, "usage: warm_metal_cache <folder> [threads] [--list <file>] [--budget <n>] | <folder> --verify <count> [--list <file>]\n"); return 2; }
        NSString *dir = @(argv[1]), *list = nil;
        int verify_count = 0, threads = 0;
        long budget = 0;
        for (int i = 2; i < argc; i++) {
            if (!strcmp(argv[i], "--verify") && i + 1 < argc) verify_count = atoi(argv[++i]) > 0 ? atoi(argv[i]) : 64;
            else if (!strcmp(argv[i], "--list") && i + 1 < argc) list = @(argv[++i]);
            else if (!strcmp(argv[i], "--budget") && i + 1 < argc) budget = atol(argv[++i]);
            else if (atoi(argv[i]) > 0) threads = atoi(argv[i]);
            else { fprintf(stderr, "warm_metal_cache: unknown argument %s\n", argv[i]); return 2; }
        }
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        if (!dev) { fprintf(stderr, "warm_metal_cache: no Metal device\n"); return 2; }
        NSArray<NSString *> *files = list ? listed_sources(list) : shader_sources(dir);
        if (verify_count) return verify(dev, files, verify_count, list == nil);
        // As the game's warm-up (nvn_mtl.m warm_threads): the core count minus 2, at most 8.
        if (threads < 1) {
            threads = (int)NSProcessInfo.processInfo.activeProcessorCount - 2;
            if (threads > 8) threads = 8;
            if (threads < 1) threads = 1;
        }
        if (budget < 0) budget = 0;
        return compile_all(dev, files, threads, budget);
    }
}
