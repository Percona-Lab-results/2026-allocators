#!/usr/bin/env python3
"""
Generate HTML report from run_allocator_perf_suite.sh results.

Scans a suite directory for results-<suffix>-<thp>-<allocator>-rep<N>-<bp>G
run directories and produces a comparative report:

  - TPM per phase (steady / regrow) as median across repetitions
  - RSS over time (mean across reps, idle phase shaded)
  - allocator overhead over time: RSS minus performance_schema tracked bytes
  - VMA count over time (from maps logs)
  - AnonHugePages over time (THP engagement, from smaps_rollup logs)
  - idle-phase RSS release and regrow ratchet bars
  - summary table with median (min..max) across reps

Usage:
  generate_suite_report.py [suite_dir] [output_file]
  generate_suite_report.py suite-allocperf1 suite_report-allocperf1.html
"""

import os
import re
import sys
import csv
import glob
import json
import argparse
import statistics
from datetime import datetime

GRID_MINUTES = 1  # time-series resample grid


def ts(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')


def find_run_dirs(suite_dir):
    """Find run directories at any depth up to 2 below suite_dir."""
    # Optional trailing storage-engine token (e.g. -48G-myrocks)
    pat = re.compile(r'^results-.*-(thp|nothp)-(\w+)-rep(\d+)-\d+G(?:-\w+)?$')
    runs = []
    for root in (suite_dir, *glob.glob(os.path.join(suite_dir, '*'))):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            m = pat.match(entry)
            d = os.path.join(root, entry)
            if m and os.path.isdir(d):
                runs.append({'dir': d, 'thp': m.group(1),
                             'allocator': m.group(2), 'rep': int(m.group(3))})
    return runs


def one(dirpath, pattern):
    files = glob.glob(os.path.join(dirpath, pattern))
    return files[0] if files else None


def parse_run(run):
    """Parse all per-run files; returns None if the run is unusable."""
    d = run['dir']
    prefix = f"{run['thp']}_{run['allocator']}"

    phases_file = one(d, f'{prefix}_phases_*.csv')
    rss_file = one(d, f'{prefix}_rss_memory_*.log')
    if not phases_file or not rss_file:
        return None

    phases = []
    with open(phases_file) as f:
        for row in csv.DictReader(f):
            phases.append((row['phase'], ts(row['start']), ts(row['end'])))
    if not phases:
        return None

    rss = []
    with open(rss_file) as f:
        for line in f:
            m = re.match(r'^([\d-]+ [\d:]+),\s*(\d+),', line)
            if m:
                rss.append((ts(m.group(1)), int(m.group(2))))
    if not rss:
        return None
    t0 = rss[0][0]

    tracked = []
    pm = one(d, f'{prefix}_ps_memory_*.csv')
    if pm:
        with open(pm) as f:
            next(f, None)
            for line in f:
                parts = line.strip().split(',')
                if len(parts) == 2 and parts[1].isdigit():
                    tracked.append((ts(parts[0]), int(parts[1])))

    # TPM over time from cumulative Com_commit + Com_rollback counters
    # (same definition HammerDB uses for "MySQL TPM")
    tpm_series = []
    tc = one(d, f'{prefix}_txn_counters_*.csv')
    if tc:
        counters = []
        with open(tc) as f:
            next(f, None)
            for line in f:
                parts = line.strip().split(',')
                if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                    counters.append((ts(parts[0]), int(parts[1]) + int(parts[2])))
        for (t1, c1), (t2, c2) in zip(counters, counters[1:]):
            mins = (t2 - t1).total_seconds() / 60.0
            if mins > 0 and c2 >= c1:
                tpm_series.append((t2, (c2 - c1) / mins))

    # VMA count per snapshot from the maps log
    vma = []
    mf = one(d, f'{prefix}_mysql_maps_*.log')
    if mf:
        cur_ts, count = None, 0
        with open(mf, errors='replace') as f:
            for line in f:
                if line.startswith('==='):
                    if cur_ts:
                        vma.append((cur_ts, count))
                    m = re.match(r'^===\s+(.+?)\s+===', line)
                    cur_ts = ts(m.group(1)) if m else None
                    count = 0
                elif cur_ts and line[:1] not in ('#', '', '\n'):
                    count += 1
        if cur_ts:
            vma.append((cur_ts, count))

    # AnonHugePages per snapshot from smaps_rollup
    ahp = []
    rf = one(d, f'{prefix}_mysql_smaps_rollup_*.log')
    if rf:
        cur_ts = None
        with open(rf, errors='replace') as f:
            for line in f:
                if line.startswith('==='):
                    m = re.match(r'^===\s+(.+?)\s+===', line)
                    cur_ts = ts(m.group(1)) if m else None
                elif cur_ts and line.startswith('AnonHugePages:'):
                    try:
                        ahp.append((cur_ts, int(line.split()[1])))
                    except (ValueError, IndexError):
                        pass
                    cur_ts = None

    def final_nopm(phase):
        f = one(d, f'{prefix}_hammerdb_{phase}_*.log')
        if not f:
            return None, None
        m = re.findall(r'achieved (\d+) NOPM from (\d+)',
                       open(f, errors='replace').read())
        return (int(m[-1][0]), int(m[-1][1])) if m else (None, None)

    nopm = {p: final_nopm(p) for p in ('steady', 'regrow')}

    def tail_median(series, start, end, n=5):
        vals = [v for t, v in series if start <= t <= end]
        return statistics.median(vals[-n:]) if vals else None

    # Per-phase stats over full-resolution rss/tracked
    phase_stats = {}
    for name, start, end in phases:
        prss = [v for t, v in rss if start <= t <= end]
        ptrk = [v for t, v in tracked if start <= t <= end]
        if not prss:
            continue
        phase_stats[name] = {
            'avg_rss_kb': sum(prss) / len(prss),
            'max_rss_kb': max(prss),
            'end_rss_kb': tail_median(rss, start, end),
            'avg_overhead_kb': (sum(prss) / len(prss) - sum(ptrk) / len(ptrk) / 1024)
                               if ptrk else None,
        }

    released = ratchet = None
    if 'steady' in phase_stats and 'idle' in phase_stats:
        released = phase_stats['steady']['end_rss_kb'] - phase_stats['idle']['end_rss_kb']
    if 'steady' in phase_stats and 'regrow' in phase_stats:
        ratchet = phase_stats['regrow']['end_rss_kb'] - phase_stats['steady']['end_rss_kb']

    def grid(series, scale=1.0):
        """Resample (datetime, value) to per-minute means from run start."""
        buckets = {}
        for t, v in series:
            m = int((t - t0).total_seconds() // 60) * GRID_MINUTES
            buckets.setdefault(m, []).append(v)
        return {m: sum(vs) / len(vs) * scale for m, vs in sorted(buckets.items())}

    tracked_grid = grid(tracked, 1 / 1024)  # bytes -> KB
    rss_grid = grid(rss)
    overhead_grid = {m: rss_grid[m] - tracked_grid[m]
                     for m in rss_grid if m in tracked_grid}

    return {
        **run,
        'phase_offsets': [(p, (s - t0).total_seconds() / 60,
                           (e - t0).total_seconds() / 60) for p, s, e in phases],
        'nopm': nopm,
        'phase_stats': phase_stats,
        'released_kb': released,
        'ratchet_kb': ratchet,
        'max_ahp_kb': max((v for _, v in ahp), default=0),
        'series': {
            'tpm': grid(tpm_series),
            'rss': rss_grid,                     # KB
            'overhead': overhead_grid,           # KB
            'vma': grid(vma),
            'ahp': grid(ahp),                    # KB
        },
    }


def mean_across_reps(runs, key):
    """Average each combo's per-minute series across its repetitions."""
    merged = {}
    for r in runs:
        for m, v in r['series'][key].items():
            merged.setdefault(m, []).append(v)
    return [{'x': m, 'y': round(sum(vs) / len(vs), 2)}
            for m, vs in sorted(merged.items())]


def med_range(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {'med': statistics.median(vals), 'min': min(vals), 'max': max(vals)}


ALLOCATOR_COLORS = {
    'glibc':      '#2a78d6',
    'jemalloc36': '#eb6834',
    'jemalloc53': '#1baf7a',
    'jemalloc54': '#1baf7a',
    'tcmalloc':   '#eda100',
}
FALLBACK_COLORS = ['#e87ba4', '#008300', '#4a3aa7', '#e34948']

LINE_CHARTS = [
    ('tpm', 'MySQL TPM over time (mean across reps)', 'TPM', 1,
     'Com_commit + Com_rollback per minute (the counters behind HammerDB\'s '
     '"MySQL TPM"), sampled every 30 s. Shows rampup, steady plateau, the '
     'idle gap and the regrow plateau; a lower second plateau indicates '
     'work the idle phase left behind (data growth, purge backlog). '
     'Requires the txn-counter log; runs from older suite versions are '
     'not shown here.'),
    ('rss', 'RSS over time (mean across reps)', 'GB', 1 / 1024 / 1024,
     'mysqld resident memory through the steady → idle → regrow phases. '
     'Idle phase is shaded; a drop there is memory the allocator returned '
     'to the OS, and a higher regrow plateau than steady is the '
     'fragmentation ratchet.'),
    ('overhead', 'Allocator overhead over time (mean across reps)', 'MB', 1 / 1024,
     'RSS minus performance_schema tracked bytes: memory the process holds '
     'beyond what the server accounts for — allocator caches, fragmentation '
     'and untracked allocations. Lower and flatter is better.'),
    ('vma', 'Mapping count over time (mean across reps)', 'VMAs', 1,
     'Number of /proc/pid/maps entries — address-space fragmentation and '
     'kernel VMA bookkeeping cost.'),
    ('ahp', 'THP-backed memory over time (mean across reps)', 'GB', 1 / 1024 / 1024,
     'AnonHugePages from smaps_rollup — how much anonymous memory each '
     'allocator keeps on transparent huge pages (thp runs only; nothp runs '
     'sit at zero by design).'),
]

BAR_CHARTS = [
    ('tpm_steady', 'TPM — steady phase (median of reps)', 'TPM',
     'HammerDB MySQL TPM in the first loaded phase.'),
    ('tpm_regrow', 'TPM — regrow phase (median of reps)', 'TPM',
     'TPM in the second loaded phase, after the idle period.'),
    ('released', 'RSS released during idle (median of reps)', 'MB',
     'RSS at end of steady minus end of idle: how much memory the allocator '
     'gave back to the OS when load stopped. Higher is better.'),
    ('ratchet', 'Regrow ratchet (median of reps)', 'MB',
     'RSS at end of regrow minus end of steady: growth of the plateau after '
     'an idle/regrow cycle — a fragmentation indicator. Lower is better.'),
]


def build_report(runs_parsed, output_file, suite_dir):
    # Group by combo
    combos = {}
    for r in runs_parsed:
        combos.setdefault((r['allocator'], r['thp']), []).append(r)

    fallback_idx = 0
    combo_list = []
    for (allocator, thp) in sorted(combos):
        rs = combos[(allocator, thp)]
        color = ALLOCATOR_COLORS.get(allocator)
        if color is None:
            color = FALLBACK_COLORS[fallback_idx % len(FALLBACK_COLORS)]
            fallback_idx += 1

        tpm_steady = med_range([r['nopm']['steady'][1] for r in rs])
        tpm_regrow = med_range([r['nopm']['regrow'][1] for r in rs])
        nopm_steady = med_range([r['nopm']['steady'][0] for r in rs])
        released = med_range([r['released_kb'] for r in rs])
        ratchet = med_range([r['ratchet_kb'] for r in rs])
        avg_rss = med_range([r['phase_stats'].get('regrow', {}).get('avg_rss_kb')
                             for r in rs])
        overhead = med_range([r['phase_stats'].get('regrow', {}).get('avg_overhead_kb')
                              for r in rs])
        max_ahp = med_range([r['max_ahp_kb'] for r in rs])

        combo_list.append({
            'label': f'{thp}-{allocator}',
            'allocator': allocator,
            'thp': thp,
            'color': color,
            'dashed': thp == 'nothp',
            'reps': len(rs),
            'series': {k: mean_across_reps(rs, k) for k, *_ in LINE_CHARTS},
            'bars': {
                'tpm_steady': tpm_steady,
                'tpm_regrow': tpm_regrow,
                'released': {k: v / 1024 for k, v in released.items()} if released else None,
                'ratchet': {k: v / 1024 for k, v in ratchet.items()} if ratchet else None,
            },
            'table': {
                'tpmSteady': tpm_steady, 'tpmRegrow': tpm_regrow,
                'nopmSteady': nopm_steady,
                'avgRssGB': round(avg_rss['med'] / 1024 / 1024, 2) if avg_rss else None,
                'overheadMB': round(overhead['med'] / 1024, 0) if overhead else None,
                'releasedMB': round(released['med'] / 1024, 0) if released else None,
                'ratchetMB': round(ratchet['med'] / 1024, 0) if ratchet else None,
                'maxAhpGB': round(max_ahp['med'] / 1024 / 1024, 2) if max_ahp else 0,
            },
        })

    # Average phase boundaries (minutes) for shading, from all runs
    idle_bounds = []
    for r in runs_parsed:
        for p, s, e in r['phase_offsets']:
            if p == 'idle':
                idle_bounds.append((s, e))
    idle_shade = None
    if idle_bounds:
        idle_shade = [round(sum(b[0] for b in idle_bounds) / len(idle_bounds), 1),
                      round(sum(b[1] for b in idle_bounds) / len(idle_bounds), 1)]

    # Omit time-series charts for which no run has data (e.g. TPM over time
    # for results collected before the txn-counter log existed)
    have_data = {k for c in combo_list for k, pts in c['series'].items() if pts}
    line_meta = [{'key': k, 'title': t, 'unit': u, 'scale': sc, 'desc': d}
                 for k, t, u, sc, d in LINE_CHARTS if k in have_data]
    bar_meta = [{'key': k, 'title': t, 'unit': u, 'desc': d}
                for k, t, u, d in BAR_CHARTS]

    sections = []
    for m in bar_meta[:2]:
        sections.append((m['key'], m['title'], m['desc']))
    for m in line_meta:
        sections.append((m['key'], m['title'], m['desc']))
    for m in bar_meta[2:]:
        sections.append((m['key'], m['title'], m['desc']))

    chart_sections = '\n'.join(
        f'''        <h2>{t}</h2>
        <p class="chart-desc">{d}</p>
        <div class="chart-container"><canvas id="chart_{k}"></canvas></div>'''
        for k, t, d in sections)

    generated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Allocator Performance Suite Report</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0; padding: 20px; background-color: #f9f9f7; color: #0b0b0b;
        }}
        .container {{
            max-width: 1100px; margin: 0 auto; background-color: #fcfcfb;
            padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ border-bottom: 3px solid #2a78d6; padding-bottom: 10px; }}
        h2 {{ color: #52514e; margin-top: 40px; }}
        .chart-desc {{ color: #52514e; font-size: 14px; margin-top: -8px; }}
        .chart-container {{ position: relative; height: 420px; margin: 20px 0 10px 0; }}
        .stats-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
        .stats-table th, .stats-table td {{
            padding: 8px 10px; text-align: right; border-bottom: 1px solid #e1e0d9;
            font-variant-numeric: tabular-nums;
        }}
        .stats-table th {{ background-color: #2a78d6; color: white; }}
        .stats-table td:first-child, .stats-table th:first-child {{ text-align: left; }}
        .stats-table tr:hover {{ background-color: #f0efec; }}
        .info-box {{
            background-color: #e7f3ff; border-left: 4px solid #2a78d6;
            padding: 15px; margin: 20px 0; font-size: 14px;
        }}
        .footer {{
            margin-top: 30px; padding-top: 20px; border-top: 1px solid #e1e0d9;
            text-align: center; color: #898781; font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Allocator Performance Suite Report</h1>

        <div class="info-box">
            <strong>Data source:</strong> run_allocator_perf_suite.sh results in
            <code>{suite_dir}</code>. Phases per run: steady load → idle →
            regrow. Time-series lines are the mean across repetitions
            (solid = THP enabled, dashed = THP disabled; color = allocator).
            Bars are the median across repetitions.
        </div>

{chart_sections}

        <h2>Summary (median across reps; TPM range in parentheses)</h2>
        <table class="stats-table">
            <thead><tr>
                <th>Configuration</th><th>Reps</th>
                <th>Steady TPM</th><th>Regrow TPM</th><th>Steady NOPM</th>
                <th>Avg RSS regrow (GB)</th><th>Overhead (MB)</th>
                <th>Idle released (MB)</th><th>Ratchet (MB)</th>
                <th>Max THP (GB)</th>
            </tr></thead>
            <tbody id="statsTableBody"></tbody>
        </table>

        <div class="footer">Generated on {generated} | generate_suite_report.py</div>
    </div>

    <script>
        const combos = {json.dumps(combo_list)};
        const lineMeta = {json.dumps(line_meta)};
        const barMeta = {json.dumps(bar_meta)};
        const idleShade = {json.dumps(idle_shade)};

        // Shade the idle phase on time-series charts
        const idleBand = {{
            id: 'idleBand',
            beforeDraw(chart) {{
                if (!idleShade) return;
                const {{ctx, chartArea, scales}} = chart;
                if (!scales.x || scales.x.type !== 'linear') return;
                const x1 = scales.x.getPixelForValue(idleShade[0]);
                const x2 = scales.x.getPixelForValue(idleShade[1]);
                ctx.save();
                ctx.fillStyle = 'rgba(137, 135, 129, 0.10)';
                ctx.fillRect(x1, chartArea.top, x2 - x1, chartArea.bottom - chartArea.top);
                ctx.restore();
            }}
        }};

        lineMeta.forEach(meta => {{
            const datasets = combos.map(c => ({{
                label: c.label,
                data: c.series[meta.key].map(p => ({{x: p.x, y: p.y * meta.scale}})),
                borderColor: c.color,
                backgroundColor: 'transparent',
                borderDash: c.dashed ? [6, 4] : [],
                tension: 0.15, pointRadius: 0, pointHitRadius: 8, borderWidth: 2,
            }}));
            new Chart(document.getElementById('chart_' + meta.key), {{
                type: 'line',
                data: {{ datasets }},
                plugins: [idleBand],
                options: {{
                    animation: false, responsive: true, maintainAspectRatio: false,
                    interaction: {{ mode: 'nearest', axis: 'x', intersect: false }},
                    plugins: {{
                        legend: {{ labels: {{ boxWidth: 30, boxHeight: 1 }} }},
                        tooltip: {{ callbacks: {{
                            title: it => it.length ? it[0].parsed.x + ' min' : '',
                            label: c2 => c2.dataset.label + ': ' +
                                c2.parsed.y.toLocaleString(undefined, {{maximumFractionDigits: 2}}) +
                                ' ' + meta.unit
                        }} }},
                    }},
                    scales: {{
                        x: {{ type: 'linear',
                              title: {{ display: true, text: 'Minutes since run start' }},
                              grid: {{ color: '#e1e0d9' }} }},
                        y: {{ title: {{ display: true, text: meta.unit }},
                              grid: {{ color: '#e1e0d9' }} }}
                    }}
                }}
            }});
        }});

        // Grouped bars: x = allocator, series = thp mode
        const allocators = [...new Set(combos.map(c => c.allocator))];
        const thpModes = [...new Set(combos.map(c => c.thp))].sort();
        const thpBarColors = {{'thp': '#2a78d6', 'nothp': '#86b6ef'}};

        barMeta.forEach(meta => {{
            const datasets = thpModes.map(mode => ({{
                label: mode,
                data: allocators.map(a => {{
                    const c = combos.find(x => x.allocator === a && x.thp === mode);
                    return c && c.bars[meta.key] ? c.bars[meta.key].med : null;
                }}),
                backgroundColor: thpBarColors[mode] || '#898781',
                borderRadius: 4, maxBarThickness: 60,
            }}));
            new Chart(document.getElementById('chart_' + meta.key), {{
                type: 'bar',
                data: {{ labels: allocators, datasets }},
                options: {{
                    animation: false, responsive: true, maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: thpModes.length > 1 }},
                        tooltip: {{ callbacks: {{
                            label: c2 => {{
                                const c = combos.find(x => x.allocator === c2.label ||
                                    (x.allocator === allocators[c2.dataIndex] && x.thp === c2.dataset.label));
                                const b = c && c.bars[meta.key];
                                const rng = b ? ' (' + Math.round(b.min).toLocaleString() +
                                    ' .. ' + Math.round(b.max).toLocaleString() + ')' : '';
                                return c2.dataset.label + ': ' +
                                    Math.round(c2.parsed.y).toLocaleString() + ' ' + meta.unit + rng;
                            }}
                        }} }},
                    }},
                    scales: {{
                        y: {{ title: {{ display: true, text: meta.unit }},
                              grid: {{ color: '#e1e0d9' }} }}
                    }}
                }}
            }});
        }});

        // Summary table
        const fmtRange = v => v ? Math.round(v.med).toLocaleString() +
            ' (' + Math.round(v.min).toLocaleString() + '..' + Math.round(v.max).toLocaleString() + ')' : '—';
        const tbody = document.getElementById('statsTableBody');
        combos.forEach(c => {{
            const t = c.table;
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${{c.label}}</td><td>${{c.reps}}</td>
                <td>${{fmtRange(t.tpmSteady)}}</td>
                <td>${{fmtRange(t.tpmRegrow)}}</td>
                <td>${{fmtRange(t.nopmSteady)}}</td>
                <td>${{t.avgRssGB ?? '—'}}</td>
                <td>${{t.overheadMB?.toLocaleString() ?? '—'}}</td>
                <td>${{t.releasedMB?.toLocaleString() ?? '—'}}</td>
                <td>${{t.ratchetMB?.toLocaleString() ?? '—'}}</td>
                <td>${{t.maxAhpGB}}</td>
            `;
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html)


def main():
    parser = argparse.ArgumentParser(
        description='Generate HTML report from allocator perf suite results.')
    parser.add_argument('suite_dir', nargs='?', default='suite-allocperf1')
    parser.add_argument('output_file', nargs='?', default='suite_report.html')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    suite_dir = args.suite_dir if os.path.isabs(args.suite_dir) \
        else os.path.join(script_dir, args.suite_dir)
    output_file = args.output_file if os.path.isabs(args.output_file) \
        else os.path.join(script_dir, args.output_file)

    runs = find_run_dirs(suite_dir)
    if not runs:
        print(f'No run directories found under {suite_dir}')
        sys.exit(1)
    print(f'Found {len(runs)} run directories.')

    parsed = []
    for run in runs:
        label = f"{run['thp']}-{run['allocator']}-rep{run['rep']}"
        print(f'Parsing {label}...', flush=True)
        r = parse_run(run)
        if r is None:
            print('  incomplete run, skipped')
            continue
        st = r['nopm']['steady'][1]
        rg = r['nopm']['regrow'][1]
        print(f"  steady TPM={st}, regrow TPM={rg}, "
              f"released={round((r['released_kb'] or 0)/1024)} MB, "
              f"ratchet={round((r['ratchet_kb'] or 0)/1024)} MB, "
              f"maxTHP={round(r['max_ahp_kb']/1024/1024, 2)} GB")
        parsed.append(r)

    if not parsed:
        print('No usable runs.')
        sys.exit(1)

    build_report(parsed, output_file, os.path.basename(suite_dir))
    print(f'\nReport generated: {output_file}')


if __name__ == '__main__':
    main()
