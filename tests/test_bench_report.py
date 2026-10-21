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
