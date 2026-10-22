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
