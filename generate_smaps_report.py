#!/usr/bin/env python3
"""
Generate HTML report with comparative /proc/<pid>/smaps analysis from
benchmark results.

Parses <THP>_<ALLOCATOR>_mysql_smaps_<timestamp>.log files (periodic dumps of
/proc/<pid>/smaps separated by "=== YYYY-MM-DD HH:MM:SS ===" markers; the
*_smaps_rollup_* files are ignored) for each THP/allocator combination and
produces a single HTML report with:

  - time-series graphs of actual memory residency (RSS, anonymous vs
    file-backed), THP effectiveness (AnonHugePages, THP coverage of
    anonymous memory) and the referenced working set
  - a summary statistics table comparing all combinations

Usage:
  generate_smaps_report.py [data_dir] [output_file]
  generate_smaps_report.py mem-2400 smaps_report-2400.html
"""

import os
import re
import sys
import glob
import json
import argparse
from datetime import datetime
from pathlib import Path

# Target number of plotted points per series.
TARGET_POINTS = 600

# smaps fields accumulated per snapshot (values are in kB in the log)
WANT_FIELDS = ('Rss', 'Pss', 'Anonymous', 'AnonHugePages', 'Referenced',
               'Swap', 'LazyFree')


def find_smaps_logs(data_dir):
    """
    Find <thp>_<allocator>_mysql_smaps_*.log (NOT smaps_rollup) in each
    results-* subdirectory. Returns dict: {result_dir_name: log_file_path}
    """
    results = {}

    for entry in sorted(os.listdir(data_dir)):
        result_dir = os.path.join(data_dir, entry)
        if not os.path.isdir(result_dir):
            continue

        pattern = os.path.join(result_dir, '*_mysql_smaps_*.log')
        log_files = [f for f in glob.glob(pattern)
                     if '_mysql_smaps_' in os.path.basename(f)
                     and '_rollup_' not in os.path.basename(f)]
        if log_files:
            results[entry] = sorted(log_files)[0]

    return results


def parse_result_dir_name(dir_name):
    """
    Extract THP mode and allocator from a results directory name, e.g.
    results-ps2400-8.4.10-thp-jemalloc53-150G-nobinlog-innodb-nopool
    """
    parts = dir_name.split('-')
    thp_idx = None
    for i, part in enumerate(parts):
        if part in ('thp', 'nothp'):
            thp_idx = i
            break

    if thp_idx is not None and thp_idx + 1 < len(parts):
        thp = parts[thp_idx]
        allocator = parts[thp_idx + 1]
        suffix = '-'.join(parts[1:thp_idx])
        return {'thp': thp, 'allocator': allocator, 'suffix': suffix,
                'label': f"{thp}-{allocator}"}

    return {'thp': 'unknown', 'allocator': 'unknown', 'suffix': dir_name,
            'label': dir_name}


def parse_smaps_log(log_file):
    """
    Stream-parse a full smaps log and return one record per snapshot:
      (timestamp, {field: total_kB, ...})
    Totals are sums over all VMAs in the snapshot.
    """
    snapshots = []
    ts = None
    acc = None

    with open(log_file, 'r', errors='replace') as f:
        for line in f:
            c = line[0] if line else '\n'

            if c == '=':
                if ts is not None and acc is not None:
                    snapshots.append((ts, acc))
                m = re.match(r'^===\s+(.+?)\s+===', line)
                ts = m.group(1) if m else None
                acc = dict.fromkeys(WANT_FIELDS, 0)
                continue

            if acc is None or c == '#' or c == '\n':
                continue

            key, sep, rest = line.partition(':')
            if sep and key in acc:
                # "Rss:               11224 kB"
                try:
                    acc[key] += int(rest.split(None, 1)[0])
                except (ValueError, IndexError):
                    pass

    if ts is not None and acc is not None:
        snapshots.append((ts, acc))
    return snapshots


def downsample(snapshots):
    """
    Convert raw snapshots into ~TARGET_POINTS chart points (bucket means).
    Returns (points, stats).
    """
    times = []
    t0 = None
    for ts, _ in snapshots:
        try:
            t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            t = None
        times.append(t)
        if t0 is None and t is not None:
            t0 = t

    n = len(snapshots)
    stride = max(1, n // TARGET_POINTS)
    points = []
    gb = 1024.0 ** 2  # kB -> GB

    for i in range(0, n, stride):
        bucket = [a for _, a in snapshots[i:i + stride]]
        bt = [t for t in times[i:i + stride] if t is not None]
        if not bt or t0 is None:
            continue
        k = len(bucket)
        mean = {f: sum(a[f] for a in bucket) / k for f in WANT_FIELDS}

        anon = mean['Anonymous']
        thp_cov = (mean['AnonHugePages'] / anon * 100.0) if anon else 0.0
        points.append({
            'x': round((bt[0] - t0).total_seconds() / 3600.0, 4),
            'rss': round(mean['Rss'] / gb, 3),
            'anon': round(anon / gb, 3),
            'filerss': round(max(mean['Rss'] - anon, 0) / gb, 3),
            'thp': round(mean['AnonHugePages'] / gb, 3),
            'thpcov': round(thp_cov, 2),
            'referenced': round(mean['Referenced'] / gb, 3),
        })

    # Summary statistics over the full-resolution data
    valid = [t for t in times if t is not None]
    duration_h = ((valid[-1] - valid[0]).total_seconds() / 3600.0
                  if len(valid) > 1 else 0.0)
    rss = [a['Rss'] for _, a in snapshots]
    anon = [a['Anonymous'] for _, a in snapshots]
    thp = [a['AnonHugePages'] for _, a in snapshots]
    refd = [a['Referenced'] for _, a in snapshots]
    covs = [(h / a * 100.0) for h, a in zip(thp, anon) if a]

    stats = {
        'snapshots': n,
        'durationH': round(duration_h, 2),
        'minRssGB': round(min(rss) / gb, 2),
        'maxRssGB': round(max(rss) / gb, 2),
        'avgRssGB': round(sum(rss) / n / gb, 2),
        'finalRssGB': round(rss[-1] / gb, 2),
        'rssGrowthGB': round((rss[-1] - rss[0]) / gb, 2),
        'avgAnonGB': round(sum(anon) / n / gb, 2),
        'avgFileRssGB': round(sum(max(r - a, 0) for r, a
                                  in zip(rss, anon)) / n / gb, 3),
        'maxThpGB': round(max(thp) / gb, 2),
        'avgThpCov': round(sum(covs) / len(covs), 1) if covs else 0.0,
        'maxSwapGB': round(max(a['Swap'] for _, a in snapshots) / gb, 2),
        'maxLazyFreeGB': round(max(a['LazyFree'] for _, a in snapshots)
                               / gb, 2),
        'avgReferencedGB': round(sum(refd) / n / gb, 2),
    }
    return points, stats


# Reference categorical palette (validated, fixed slot order); allocator
# carries the hue, THP mode carries the line style (solid vs dashed).
ALLOCATOR_COLORS = {
    'glibc':      '#2a78d6',   # blue
    'jemalloc36': '#eb6834',   # orange
    'jemalloc53': '#1baf7a',   # aqua
    'jemalloc54': '#1baf7a',
    'tcmalloc':   '#eda100',   # yellow
}
FALLBACK_COLORS = ['#e87ba4', '#008300', '#4a3aa7', '#e34948']

CHARTS = [
    ('rss', 'Total RSS', 'GB',
     'Sum of Rss over all mappings — physical memory the process actually '
     'occupies. The ground truth the allocators are compared on.'),
    ('anon', 'Anonymous RSS', 'GB',
     'Resident anonymous pages (allocator heaps, buffer pool, thread '
     'stacks). This is the memory the allocator is responsible for.'),
    ('filerss', 'File-backed RSS', 'GB',
     'Rss − Anonymous: resident pages of mapped files (binary, libraries, '
     'mapped data files).'),
    ('thp', 'THP-backed memory (AnonHugePages)', 'GB',
     'Anonymous memory currently backed by transparent huge pages. Zero in '
     'a THP-enabled run means THP never materialized (e.g. THP in madvise '
     'mode without MADV_HUGEPAGE from the allocator).'),
    ('thpcov', 'THP coverage of anonymous memory', '%',
     'AnonHugePages / Anonymous — the share of allocator-managed memory '
     'that actually uses huge pages.'),
    ('referenced', 'Referenced memory', 'GB',
     'Pages the kernel saw accessed since the last reclaim scan — an upper '
     'bound on the active working set.'),
]


def generate_html_report(results, output_file, data_dir):
    """
    results: {dir_name: (config, points, stats)}
    """
    combos = []
    fallback_idx = 0
    for dir_name in sorted(results,
                           key=lambda d: (results[d][0]['allocator'],
                                          results[d][0]['thp'])):
        config, points, stats = results[dir_name]
        color = ALLOCATOR_COLORS.get(config['allocator'])
        if color is None:
            color = FALLBACK_COLORS[fallback_idx % len(FALLBACK_COLORS)]
            fallback_idx += 1
        combos.append({
            'label': config['label'],
            'thp': config['thp'],
            'allocator': config['allocator'],
            'dir': dir_name,
            'color': color,
            'dashed': config['thp'] == 'nothp',
            'points': points,
            'stats': stats,
        })

    charts_meta = [{'key': k, 'title': t, 'unit': u, 'desc': d}
                   for k, t, u, d in CHARTS]

    chart_sections = '\n'.join(
        f'''        <h2>{t}</h2>
        <p class="chart-desc">{d}</p>
        <div class="chart-container"><canvas id="chart_{k}"></canvas></div>
        <button class="reset-zoom-button" data-chart="{k}">Reset Zoom</button>'''
        for k, t, u, d in CHARTS)

    generated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MySQL /proc/pid/smaps Analysis — THP × Allocator</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f9f9f7;
            color: #0b0b0b;
        }}
        .container {{
            max-width: 1100px;
            margin: 0 auto;
            background-color: #fcfcfb;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{ border-bottom: 3px solid #2a78d6; padding-bottom: 10px; }}
        h2 {{ color: #52514e; margin-top: 40px; }}
        .chart-desc {{ color: #52514e; font-size: 14px; margin-top: -8px; }}
        .chart-container {{ position: relative; height: 480px; margin: 20px 0 10px 0; }}
        .stats-table {{
            width: 100%; border-collapse: collapse; margin: 20px 0;
            font-size: 13px;
        }}
        .stats-table th, .stats-table td {{
            padding: 8px 10px; text-align: right;
            border-bottom: 1px solid #e1e0d9;
            font-variant-numeric: tabular-nums;
        }}
        .stats-table th {{ background-color: #2a78d6; color: white; }}
        .stats-table td:first-child, .stats-table th:first-child {{ text-align: left; }}
        .stats-table tr:hover {{ background-color: #f0efec; }}
        .info-box {{
            background-color: #e7f3ff; border-left: 4px solid #2a78d6;
            padding: 15px; margin: 20px 0; font-size: 14px;
        }}
        .controls {{
            margin: 20px 0; padding: 15px; border-radius: 5px;
            border: 1px solid #e1e0d9; position: sticky; top: 0;
            background-color: #fcfcfb; z-index: 10;
        }}
        .controls h3 {{ margin: 0 0 8px 0; color: #52514e; font-size: 14px; }}
        .checkbox-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 6px;
        }}
        .checkbox-item {{ display: flex; align-items: center; }}
        .checkbox-item input {{ margin-right: 8px; cursor: pointer; }}
        .checkbox-item label {{
            cursor: pointer; user-select: none;
            display: flex; align-items: center; font-size: 13px;
        }}
        .line-swatch {{
            display: inline-block; width: 28px; height: 0;
            border-top-width: 3px; margin-right: 8px;
        }}
        .reset-zoom-button {{
            padding: 6px 14px; background-color: #52514e; color: white;
            border: none; border-radius: 5px; cursor: pointer; font-size: 13px;
        }}
        .reset-zoom-button:hover {{ background-color: #0b0b0b; }}
        .footer {{
            margin-top: 30px; padding-top: 20px; border-top: 1px solid #e1e0d9;
            text-align: center; color: #898781; font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>MySQL /proc/pid/smaps Analysis — THP × Allocator</h1>

        <div class="info-box">
            <strong>Data source:</strong> periodic <code>/proc/&lt;pid&gt;/smaps</code>
            snapshots of mysqld from <code>&lt;thp&gt;_&lt;allocator&gt;_mysql_smaps_*.log</code>
            in <code>{data_dir}</code> (rollup files ignored; per-VMA fields
            summed per snapshot).
            Solid lines = THP enabled, dashed lines = THP disabled; color = allocator.
            Drag on a chart to zoom.
        </div>

        <div class="controls">
            <h3>Configurations</h3>
            <div class="checkbox-grid" id="comboToggles"></div>
        </div>

{chart_sections}

        <h2>Summary statistics</h2>
        <table class="stats-table">
            <thead><tr>
                <th>Configuration</th>
                <th>Duration (h)</th>
                <th>Snapshots</th>
                <th>Min RSS (GB)</th>
                <th>Max RSS (GB)</th>
                <th>Avg RSS (GB)</th>
                <th>Final RSS (GB)</th>
                <th>RSS growth (GB)</th>
                <th>Avg anon (GB)</th>
                <th>Avg file RSS (GB)</th>
                <th>Max THP (GB)</th>
                <th>Avg THP cov (%)</th>
                <th>Avg referenced (GB)</th>
                <th>Max swap (GB)</th>
            </tr></thead>
            <tbody id="statsTableBody"></tbody>
        </table>

        <div class="footer">
            Generated on {generated} | generate_smaps_report.py
        </div>
    </div>

    <script>
        const combos = {json.dumps(combos)};
        const chartsMeta = {json.dumps(charts_meta)};

        const charts = {{}};
        chartsMeta.forEach(meta => {{
            const datasets = combos.map(c => ({{
                label: c.label,
                data: c.points.map(p => ({{x: p.x, y: p[meta.key]}})),
                borderColor: c.color,
                backgroundColor: 'transparent',
                borderDash: c.dashed ? [6, 4] : [],
                tension: 0.15,
                pointRadius: 0,
                pointHitRadius: 8,
                borderWidth: 2,
            }}));

            const ctx = document.getElementById('chart_' + meta.key).getContext('2d');
            charts[meta.key] = new Chart(ctx, {{
                type: 'line',
                data: {{ datasets }},
                options: {{
                    animation: false,
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ mode: 'nearest', axis: 'x', intersect: false }},
                    plugins: {{
                        legend: {{
                            display: true,
                            labels: {{ usePointStyle: false, boxWidth: 30, boxHeight: 1 }}
                        }},
                        tooltip: {{
                            callbacks: {{
                                title: items => items.length
                                    ? (items[0].parsed.x).toFixed(2) + ' h elapsed' : '',
                                label: ctx2 => ctx2.dataset.label + ': ' +
                                    ctx2.parsed.y.toLocaleString() + ' ' + meta.unit
                            }}
                        }},
                        zoom: {{
                            pan: {{ enabled: true, mode: 'xy', modifierKey: 'shift' }},
                            zoom: {{
                                drag: {{
                                    enabled: true,
                                    backgroundColor: 'rgba(42, 120, 214, 0.15)',
                                    borderColor: '#2a78d6',
                                    borderWidth: 1,
                                }},
                                mode: 'xy',
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            type: 'linear',
                            title: {{ display: true, text: 'Elapsed time (hours)' }},
                            grid: {{ color: '#e1e0d9' }},
                        }},
                        y: {{
                            title: {{ display: true, text: meta.unit }},
                            beginAtZero: true,
                            grid: {{ color: '#e1e0d9' }},
                        }}
                    }}
                }}
            }});
        }});

        // Per-configuration visibility toggles (apply to every chart)
        const grid = document.getElementById('comboToggles');
        combos.forEach((c, idx) => {{
            const div = document.createElement('div');
            div.className = 'checkbox-item';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = 'combo-' + idx;
            cb.checked = true;
            cb.addEventListener('change', function() {{
                Object.values(charts).forEach(ch => {{
                    ch.data.datasets[idx].hidden = !this.checked;
                    ch.update();
                }});
            }});
            const label = document.createElement('label');
            label.htmlFor = cb.id;
            const swatch = document.createElement('span');
            swatch.className = 'line-swatch';
            swatch.style.borderTopColor = c.color;
            swatch.style.borderTopStyle = c.dashed ? 'dashed' : 'solid';
            const text = document.createElement('span');
            text.textContent = c.label;
            label.appendChild(swatch);
            label.appendChild(text);
            div.appendChild(cb);
            div.appendChild(label);
            grid.appendChild(div);
        }});

        document.querySelectorAll('.reset-zoom-button').forEach(btn => {{
            btn.addEventListener('click', () => charts[btn.dataset.chart].resetZoom());
        }});

        // Summary table
        const tbody = document.getElementById('statsTableBody');
        combos.forEach(c => {{
            const s = c.stats;
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${{c.label}}</td>
                <td>${{s.durationH}}</td>
                <td>${{s.snapshots.toLocaleString()}}</td>
                <td>${{s.minRssGB}}</td>
                <td>${{s.maxRssGB}}</td>
                <td>${{s.avgRssGB}}</td>
                <td>${{s.finalRssGB}}</td>
                <td>${{s.rssGrowthGB}}</td>
                <td>${{s.avgAnonGB}}</td>
                <td>${{s.avgFileRssGB}}</td>
                <td>${{s.maxThpGB}}</td>
                <td>${{s.avgThpCov}}</td>
                <td>${{s.avgReferencedGB}}</td>
                <td>${{s.maxSwapGB}}</td>
            `;
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html_content)


def main():
    parser = argparse.ArgumentParser(
        description='Generate comparative HTML report from mysql_smaps logs '
                    'for each THP/allocator combination.')
    parser.add_argument('data_dir', nargs='?', default='mem-2400',
                        help='Directory with results-* subdirectories '
                             '(default: mem-2400)')
    parser.add_argument('output_file', nargs='?', default='smaps_report.html',
                        help='Output HTML file (default: smaps_report.html)')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = args.data_dir if os.path.isabs(args.data_dir) \
        else os.path.join(script_dir, args.data_dir)
    output_file = args.output_file if os.path.isabs(args.output_file) \
        else os.path.join(script_dir, args.output_file)

    if not os.path.exists(data_dir):
        print(f"Error: data directory does not exist: {data_dir}")
        sys.exit(1)

    log_files = find_smaps_logs(data_dir)
    if not log_files:
        print(f"No *_mysql_smaps_*.log files found under {data_dir}")
        sys.exit(1)

    print(f"Found {len(log_files)} result directories with smaps logs.\n")

    results = {}
    for dir_name, log_file in log_files.items():
        config = parse_result_dir_name(dir_name)
        size_gb = os.path.getsize(log_file) / 1024**3
        print(f"Processing {config['label']:22s} "
              f"({os.path.basename(log_file)}, {size_gb:.1f} GB)...",
              flush=True)
        snapshots = parse_smaps_log(log_file)
        if not snapshots:
            print("  No snapshots parsed, skipping.")
            continue
        points, stats = downsample(snapshots)
        print(f"  {stats['snapshots']} snapshots over {stats['durationH']} h, "
              f"avg RSS {stats['avgRssGB']} GB, "
              f"max THP {stats['maxThpGB']} GB, "
              f"avg THP coverage {stats['avgThpCov']}%")
        results[dir_name] = (config, points, stats)

    if not results:
        print("No data parsed; report not generated.")
        sys.exit(1)

    generate_html_report(results, output_file, os.path.basename(data_dir))
    print(f"\nReport generated: {output_file}")
    print(f"Open in browser: file://{output_file}")


if __name__ == '__main__':
    main()
