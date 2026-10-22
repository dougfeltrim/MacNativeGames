"""Packaging-time Metal warm-up (build_app.step_warm_metal, games/totk/tools/warm_metal_cache.m)."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

import build_app

TOOL_SOURCE = Path(build_app.GAME) / 'tools/warm_metal_cache.m'


class StepWarmMetalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.tree, self.out, self.logs = root / 'tree', root / 'Game.app', root / 'logs'
        self.logs.mkdir()
        (self.tree / 'build').mkdir(parents=True)
        (self.tree / 'build/warm-metal-cache').write_text('tool')
        self.app_build = self.out / 'Contents/Resources/totk/build'
        self.app_build.mkdir(parents=True)
        self.app_tool = self.app_build / 'warm-metal-cache'
        self.said = []

    def tearDown(self):
        self.folder.cleanup()

    def step(self, game_running=False, reports=None, fail=False, users=(), users_after=(), ram=32 << 30, warm_threads=None,
             warm_budget=None):
        """Run the step with pgrep, lsof and the tool replaced; reports: {log name: JSON object} the tool would print;
        users / users_after: the programs that have Metal's cache open before compiling / when the check failed;
        ram: this Mac's physical RAM for the thread choice (the tests simulate a 32 GB machine unless told otherwise);
        warm_threads: the TOTK_BUILD_WARM_THREADS environment variable (an explicit count wins over the RAM default)."""
        calls = []
        cache_users = iter([list(users), list(users_after)])
        env = {}
        if warm_threads is not None:
            env['TOTK_BUILD_WARM_THREADS'] = warm_threads
        if warm_budget is not None:
            env['TOTK_BUILD_WARM_BUDGET'] = warm_budget

        def run(cmd, log, **_kwargs):
            calls.append([str(c) for c in cmd])
            self.assertTrue(self.app_tool.is_file(), 'the tool runs from the game binary\'s folder')
            if fail:
                raise build_app.BuildError('warm-metal-cache failed (exit 2)')
            log.write_text('warm_metal_cache: 1000 / 8290 after 20 s\n' + json.dumps((reports or {}).get(log.name, {})) + '\n')

        def system(cmd, **_kwargs):
            if cmd[0] == 'pgrep':
                return mock.Mock(returncode=0 if game_running else 1)
            raise AssertionError(f'unexpected command {cmd}')

        with mock.patch.object(build_app.subprocess, 'run', side_effect=system), mock.patch.object(build_app, 'run', side_effect=run), \
             mock.patch.object(build_app, 'clone', side_effect=lambda src, dst: shutil.copyfile(src, dst)), \
             mock.patch.object(build_app, 'say', side_effect=self.said.append), \
             mock.patch.object(build_app, 'phys_ram_bytes', return_value=ram), \
             mock.patch.dict(build_app.os.environ, env), \
             mock.patch.object(build_app, 'metal_cache_users', side_effect=lambda: next(cache_users)):
            build_app.step_warm_metal(self.tree, self.out, self.logs)
        return calls

    def test_skipped_while_a_game_is_running(self):
        self.assertEqual(self.step(game_running=True), [])
        self.assertIn("a game has Metal's shader cache open", self.said[-1])

    def test_skipped_while_another_program_has_the_cache_open(self):
        self.assertEqual(self.step(users=['llama-server (4242)']), [])
        self.assertIn("llama-server (4242) has Metal's shader cache open", self.said[-1])
        self.assertFalse(self.app_tool.exists())

    def test_runs_from_the_game_binarys_folder_and_leaves_nothing_there(self):
        calls = self.step(reports={
            'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'cold': 8000, 'hits': 290, 'skipped': 0, 'failed': 0,
                                     'threads': 8, 'seconds': 170.0},
            'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}})
        seed, order = str(self.app_build / 'shader-seed'), str(self.logs / 'warm-order.txt')
        self.assertEqual(calls, [[str(self.app_tool), seed, '--verify', '64', '--list', order],
                                 ['caffeinate', '-i', str(self.app_tool), seed, '--list', order, '--budget', '0'],
                                 [str(self.app_tool), seed, '--verify', '64']])
        self.assertFalse(self.app_tool.exists(), 'the tool must not stay inside the signed app')
        self.assertIn('8290 shaders (8000 compiled, 290 already in the store) checked', self.said[-1])
        self.assertIn("finds them in Metal's cache (64 of 64", self.said[-1])

    def test_low_ram_uses_the_measured_thread_count_and_points_at_the_toolchain(self):
        """Six threads measured fastest on an 8 GB M1 (eight was slower and swapped); a 4-core 8 GB Mac gets four."""
        calls = self.step(ram=8 << 30, reports={
            'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0, 'threads': 6, 'seconds': 700.0},
            'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}})
        seed, order = str(self.app_build / 'shader-seed'), str(self.logs / 'warm-order.txt')
        self.assertEqual(calls[1], ['caffeinate', '-i', str(self.app_tool), seed, '6', '--list', order, '--budget', '0'])
        self.assertEqual(calls[2], [str(self.app_tool), seed, '--verify', '64'])
        self.assertTrue(any('6 compiler threads' in s for s in self.said))
        self.assertTrue(any('MetalToolchain' in s for s in self.said))
        with mock.patch.object(build_app.os, 'cpu_count', return_value=4):
            calls = self.step(ram=8 << 30, reports={
                'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0, 'threads': 4, 'seconds': 700.0},
                'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}})
        self.assertEqual(calls[1][4], '4')

    def test_explicit_thread_count_wins_over_the_ram_default(self):
        """TOTK_BUILD_WARM_THREADS is the escape hatch for a machine whose compiler threads do fit in its RAM."""
        reports = {'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0, 'threads': 8, 'seconds': 300.0},
                   'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}}
        seed, order = str(self.app_build / 'shader-seed'), str(self.logs / 'warm-order.txt')
        self.assertEqual(self.step(ram=8 << 30, warm_threads='8', reports=reports)[1],
                         ['caffeinate', '-i', str(self.app_tool), seed, '8', '--list', order, '--budget', '0'])
        self.assertTrue(any('8 compiler threads (TOTK_BUILD_WARM_THREADS)' in s for s in self.said))
        self.assertFalse(any('two compiler threads' in s for s in self.said), 'the RAM default must not also speak')
        self.assertEqual(self.step(ram=8 << 30, warm_threads='0', reports=reports)[1],
                         ['caffeinate', '-i', str(self.app_tool), seed, '6', '--list', order, '--budget', '0'],
                         'a zero or malformed count is not a thread count; the RAM default stands')

    def test_the_title_shaders_already_in_the_cache_skip_the_compile(self):
        """A rebuild in the same place is free: the game's own lookup is a fraction of a millisecond per shader."""
        calls = self.step(reports={
            'warm-metal-cache-preverify.log': {'sampled': 64, 'hits': 63, 'median_ms': 0.2, 'max_ms': 1.1}})
        self.assertEqual(len(calls), 1, 'no compile after the probe found the title shaders')
        self.assertIn('already in Metal\'s cache (63 of 64', self.said[-1])
        self.assertFalse(self.app_tool.exists())

    def test_budget_comes_from_the_environment(self):
        reports = {'warm-metal-cache.log': {'files': 8290, 'compiled': 7000, 'cold': 7000, 'skipped': 0, 'failed': 0,
                                            'duplicates': 0, 'budget_stopped': True, 'remaining': 1290},
                   'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}}
        calls = self.step(warm_budget='12000', reports=reports)
        self.assertEqual(calls[1][-2:], ['--budget', '12000'])
        self.assertTrue(any('budget of 12000' in s for s in self.said))

    def test_says_how_many_duplicates_were_not_compiled_twice(self):
        self.step(reports={
            'warm-metal-cache.log': {'files': 8290, 'compiled': 6219, 'cold': 6219, 'duplicates': 2071, 'skipped': 0, 'failed': 0},
            'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 64, 'median_ms': 0.3, 'max_ms': 0.9}})
        self.assertTrue(any('2071 files are byte-identical' in s for s in self.said))

    def test_says_so_when_metal_did_not_keep_them(self):
        self.step(reports={
            'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0},
            'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 3, 'median_ms': 61.0, 'max_ms': 95.0}})
        self.assertIn('Metal did not keep them (3 of 64', self.said[-1])
        self.assertIn('another program probably used', self.said[-1])

    def test_names_a_program_that_opened_the_cache_meanwhile(self):
        self.step(reports={
            'warm-metal-cache.log': {'files': 8290, 'compiled': 8290, 'skipped': 0, 'failed': 0},
            'warm-metal-cache-verify.log': {'sampled': 64, 'hits': 1, 'median_ms': 48.0, 'max_ms': 250.0}},
            users_after=['tool (777)'])
        self.assertIn("tool (777) has Metal's cache open now", self.said[-1])

    def test_a_failing_tool_does_not_fail_the_build_and_is_removed(self):
        self.step(fail=True)
        self.assertIn('the first start compiles them instead', self.said[-1])
        self.assertFalse(self.app_tool.exists())

    def test_a_tool_left_by_an_interrupted_build_is_removed(self):
        self.app_tool.write_text('left over')
        self.step(game_running=True)
        self.assertFalse(self.app_tool.exists())

    def test_last_json_takes_the_report_line(self):
        log = self.logs / 'x.log'
        log.write_text('{"old": 1}\nnoise {\n{"compiled": 2}\nwarm_metal_cache: done\n')
        self.assertEqual(build_app.last_json(log), {'compiled': 2})
        log.write_text('nothing\n')
        self.assertIsNone(build_app.last_json(log))



class WarmOrderTests(unittest.TestCase):
    """warm_order: the title list first, then the game's manifest, then the seed's, the largest file first in each
    group; keys, not answers hashes, find the seed's files."""

    KEY_A, KEY_B, KEY_C, KEY_D = ('a' * 64, 'b' * 64, 'c' * 64, 'd' * 64)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.seed, self.build = root / 'seed', root / 'build'
        self.seed.mkdir()
        self.build.mkdir()

    def tearDown(self):
        self.folder.cleanup()

    def source(self, key, answers, size):
        path = self.seed / f'{key}-{answers}_b.metal'
        path.write_text('x' * size)
        return path

    def test_groups_and_size_within_each(self):
        a_small = self.source(self.KEY_A, '1111', 10)
        b_small = self.source(self.KEY_B, '2222', 20)
        b_big = self.source(self.KEY_B, '3333', 40)
        c = self.source(self.KEY_C, '4444', 5)
        d = self.source(self.KEY_D, '5555', 7)
        packed = bytes(f'{self.KEY_B}-8888_b', 'ascii') + b'\0' + bytes(f'{self.KEY_A}-9999_b', 'ascii')
        (self.build / 'pso_title.tsv').write_text(
            f'# title\nS\t{self.KEY_A}-7777_b\t0\nR\t{packed.hex()}\t0\nS\tnot-a-shader\t0\nbroken\n')
        (self.build / 'pso_manifest.tsv').write_text(f'S\t{self.KEY_B}-6666_b\t0\n')
        (self.seed / 'pso_manifest.tsv').write_text(f'C\t{c.name[:-len(".metal")]}\nS\tmissing-0000_b\n')
        self.assertEqual(build_app.warm_order(self.seed, self.build), [b_big, b_small, a_small, c, d],
                         'title files first (the R row names a title key too), then the game manifest, then the seed '
                         'manifest, then the rest; big files before small ones inside a group')

    def test_no_lists_still_orders_every_file(self):
        one = self.source(self.KEY_A, '1111', 3)
        two = self.source(self.KEY_B, '2222', 9)
        self.assertEqual(build_app.warm_order(self.seed, self.build), [two, one])


class MetalCacheUsersTests(unittest.TestCase):
    """metal_cache_users: lsof on the shared store's files, parsed into 'name (pid)'."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = Path(self.folder.name)
        self.commands = []

    def tearDown(self):
        self.folder.cleanup()

    def users(self, lsof_output=''):
        def run(cmd, **_kwargs):
            self.commands.append(cmd)
            if cmd[0] == 'getconf':
                return mock.Mock(returncode=0, stdout=str(self.cache) + '/\n')
            if cmd[0] == 'lsof':
                return mock.Mock(returncode=0 if lsof_output else 1, stdout=lsof_output)
            raise AssertionError(f'unexpected command {cmd}')
        with mock.patch.object(build_app.subprocess, 'run', side_effect=run):
            return build_app.metal_cache_users()

    def test_no_store_no_lsof(self):
        self.assertEqual(self.users(), [])
        self.assertEqual([c[0] for c in self.commands], ['getconf'])

    def test_parses_the_programs_holding_it(self):
        store = self.cache / 'com.apple.metal/32024'
        store.mkdir(parents=True)
        for name in ('libraries.data', 'libraries.list'):
            (store / name).write_bytes(b'')
        found = self.users('p96998\ncmetal-cache-hold\nf4\nf5\nftxt\np100\nctotk-native\nf9\n')
        self.assertEqual(found, ['metal-cache-hold (96998)', 'totk-native (100)'])
        lsof = self.commands[-1]
        self.assertEqual(lsof[:4], ['lsof', '-F', 'pc', '--'])
        self.assertEqual(sorted(Path(f).name for f in lsof[4:]), ['libraries.data', 'libraries.list'])

    def test_nobody_holding_it(self):
        store = self.cache / 'com.apple.metal/32024'
        store.mkdir(parents=True)
        (store / 'libraries.data').write_bytes(b'')
        self.assertEqual(self.users(''), [])

@unittest.skipUnless(shutil.which('xcrun'), 'needs the Xcode command line tools')
class WarmMetalToolTests(unittest.TestCase):
    """The real tool on three small sources: one compiles, one the game skips (double), one does not compile."""

    def build(self, root):
        tool = root / 'warm-metal-cache'
        built = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-Werror', str(TOOL_SOURCE),
                                '-framework', 'Metal', '-framework', 'Foundation', '-o', str(tool)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        return tool

    def run_tool(self, cmd):
        r = subprocess.run([str(part) for part in cmd], capture_output=True, text=True)
        if r.returncode == 2 and 'no Metal device' in r.stderr:
            self.skipTest('no Metal device')
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr

    def test_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tool = self.build(root)
            seed = root / 'seed'
            seed.mkdir()
            # a unique comment makes the text new here, so the run compiles it whatever the Mac's store holds
            body = ('#include <metal_stdlib>\nusing namespace metal;\n'
                    'kernel void mng_test_warm(device float *a [[buffer(0)]], uint i [[thread_position_in_grid]]) { a[i] = a[i] * 2.0f; }\n'
                    f'// {uuid.uuid4().hex}\n')
            (seed / 'a_b.metal').write_text(body)
            (seed / 'd_b.metal').write_text(body)   # byte-identical: compiled once
            (seed / 'b_b.metal').write_text('kernel void k(device double *a [[buffer(0)]]) {}\n')
            (seed / 'c_b.metal').write_text('this is not metal\n')
            (seed / 'a_b.json').write_text('{}')   # not a source: ignored
            report, _ = self.run_tool([tool, seed, '2'])
            self.assertEqual((report['files'], report['unique'], report['duplicates'], report['compiled'],
                              report['skipped'], report['failed']), (4, 3, 1, 1, 1, 1))
            v, _ = self.run_tool([tool, seed, '--verify', '3'])
            self.assertEqual(v['sampled'], 1)

    def test_list_budget_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tool = self.build(root)
            seed = root / 'seed'
            seed.mkdir()
            names = []
            for n in ('one', 'two', 'three'):
                (seed / f'{n}_b.metal').write_text(
                    '#include <metal_stdlib>\nusing namespace metal;\n'
                    f'kernel void mng_test_{n}(device float *a [[buffer(0)]]) {{ a[0] = 1.0f; }}\n// {uuid.uuid4().hex}\n')
                names.append(str(seed / f'{n}_b.metal'))
            listing = root / 'order.txt'
            listing.write_text('# comment\n\n' + '\n'.join(names) + '\n')
            report, _ = self.run_tool([tool, seed, '1', '--list', listing, '--budget', '1'])
            self.assertEqual((report['files'], report['unique'], report['cold'], report['budget_stopped']),
                             (3, 3, 1, True))
            self.assertEqual(report['remaining'], 2, 'the one-thread worker stops before claiming another file')
            v, _ = self.run_tool([tool, seed, '--verify', '2', '--list', listing])
            self.assertEqual((v['sampled'], v['hits']), (2, 1), 'the first two files of the list: one was compiled, '
                                                               'the other one is still cold')


if __name__ == '__main__':
    unittest.main()


class MetalCompilerTests(unittest.TestCase):
    """The offline compiler comes with an Xcode's Metal toolchain; the selected developer folder may be the command line
    tools, so other Xcodes are tried and the one that has it is used to run it too."""

    def fake_run(self, found_in):
        def run(argv, capture_output=True, text=True, env=None):
            developer = (env or {}).get('DEVELOPER_DIR')
            if argv[:2] == ['xcrun', '--find']:
                ok = developer == found_in
                return subprocess.CompletedProcess(argv, 0 if ok else 1, stdout='/toolchain/metal\n' if ok else '', stderr='')
            return subprocess.CompletedProcess(argv, 0, stdout='Apple metal version 32023\n', stderr='')
        return run

    def test_an_xcode_outside_the_selected_folder_supplies_the_compiler(self):
        with mock.patch.object(build_app, 'developer_dirs', return_value=['/Library/Developer/CommandLineTools',
                                                                          '/Volumes/x/Xcode.app/Contents/Developer']), \
             mock.patch.object(build_app.subprocess, 'run', side_effect=self.fake_run('/Volumes/x/Xcode.app/Contents/Developer')), \
             mock.patch.dict(build_app.os.environ, {}, clear=False):
            build_app.os.environ.pop('DEVELOPER_DIR', None)
            metal, version, env = build_app.metal_compiler()
        self.assertEqual((metal, version, env['DEVELOPER_DIR']),
                         ('/toolchain/metal', 'Apple metal version 32023', '/Volumes/x/Xcode.app/Contents/Developer'))

    def test_no_toolchain_anywhere_is_none(self):
        with mock.patch.object(build_app, 'developer_dirs', return_value=['/Library/Developer/CommandLineTools']), \
             mock.patch.object(build_app.subprocess, 'run', side_effect=self.fake_run('nowhere')):
            self.assertIsNone(build_app.metal_compiler())


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
