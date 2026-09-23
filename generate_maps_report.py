#!/usr/bin/env python3
"""
Generate HTML report with comparative /proc/<pid>/maps analysis from
benchmark results.

Parses <THP>_<ALLOCATOR>_mysql_maps_<timestamp>.log files (periodic dumps of
/proc/<pid>/maps separated by "=== YYYY-MM-DD HH:MM:SS ===" markers) for each
THP/allocator combination and produces a single HTML report with:

  - time-series graphs (progression of memory allocation efficiency and
    memory-related operations: committed anon, reserved PROT_NONE, total
    mapped, VMA count, mapping churn, committed/mapped efficiency)
  - a summary statistics table comparing all combinations

Usage:
  generate_maps_report.py [data_dir] [output_file]
  generate_maps_report.py mem-2400 maps_report-2400.html
"""

import os
import re
import sys
import glob
import json
import argparse
from datetime import datetime
from pathlib import Path

# Target number of plotted points per series (raw snapshots are bucketed
# down to roughly this many so the browser stays responsive).
TARGET_POINTS = 600


def find_maps_logs(data_dir):
    """
    Find <thp>_<allocator>_mysql_maps_*.log in each results-* subdirectory.
    Returns dict: {result_dir_name: log_file_path}
    """
    results = {}

    for entry in sorted(os.listdir(data_dir)):
        result_dir = os.path.join(data_dir, entry)
        if not os.path.isdir(result_dir):
            continue

        pattern = os.path.join(result_dir, '*_mysql_maps_*.log')
        # Exclude smaps / smaps_rollup files which also match "*maps*"
        log_files = [f for f in glob.glob(pattern)
                     if '_mysql_maps_' in os.path.basename(f)]
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


def parse_maps_log(log_file):
    """
    Stream-parse a maps log and return one record per snapshot:
      (timestamp, vmas, total_bytes, committed_bytes, reserved_bytes,
       file_bytes, added, removed)

    committed = writable anonymous regions + [heap] + [stack] (memory the
                allocator actually handed out / touched-able)
    reserved  = PROT_NONE (---p) regions (address space the allocator set
                aside but not made usable, e.g. arena tails / guard ranges)
    """
    snapshots = []
    ts = None
    vmas = 0
    total = committed = reserved = filesz = 0
    cur_ranges = []
    prev_set = set()

    def flush():
        nonlocal prev_set
        if ts is None:
            return
        cur_set = set(cur_ranges)
        added = len(cur_set - prev_set)
        removed = len(prev_set - cur_set)
        snapshots.append((ts, vmas, total, committed, reserved,
                          filesz, added, removed))
        prev_set = cur_set

    with open(log_file, 'r', errors='replace') as f:
        for line in f:
            c = line[0] if line else '\n'

            if c == '=':
                # "=== 2026-08-19 05:54:40 ===" snapshot separator
                flush()
                m = re.match(r'^===\s+(.+?)\s+===', line)
                ts = m.group(1) if m else None
                vmas = 0
                total = committed = reserved = filesz = 0
                cur_ranges = []
                continue

            if c == '#' or c == '\n':
                continue

            # "start-end perms offset dev inode [path]"
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            addr = parts[0]
            dash = addr.find('-')
            try:
                size = int(addr[dash + 1:], 16) - int(addr[:dash], 16)
            except ValueError:
                continue

            perms = parts[1]
            path = parts[5] if len(parts) > 5 else ''

            vmas += 1
            total += size
            cur_ranges.append(addr)

            if path:
                if path[0] == '/':
                    filesz += size
                elif path[0] == '[':
                    # [heap], [stack] count as committed program memory
                    if path.startswith('[heap') or path.startswith('[stack'):
                        committed += size
            else:
                # anonymous mapping
                if perms.startswith('---'):
                    reserved += size
                elif 'w' in perms:
                    committed += size

    flush()
    return snapshots


def downsample(snapshots):
    """
    Convert raw snapshots into ~TARGET_POINTS chart points.
    Sizes/counts are averaged inside each bucket; churn (added/removed)
    is summed and converted to events per minute.
    Returns (points, stats).
    """
    times = []
    t0 = None
    for s in snapshots:
        try:
            t = datetime.strptime(s[0], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            t = None
        times.append(t)
        if t0 is None and t is not None:
            t0 = t

    n = len(snapshots)
    stride = max(1, n // TARGET_POINTS)
    points = []

    for i in range(0, n, stride):
        bucket = snapshots[i:i + stride]
        bt = [t for t in times[i:i + stride] if t is not None]
        if not bt or t0 is None:
            continue
        elapsed_h = (bt[0] - t0).total_seconds() / 3600.0
        span_min = max((bt[-1] - bt[0]).total_seconds() / 60.0, 1e-9)
        k = len(bucket)

        avg_vmas = sum(b[1] for b in bucket) / k
        avg_total = sum(b[2] for b in bucket) / k
        avg_comm = sum(b[3] for b in bucket) / k
        avg_resv = sum(b[4] for b in bucket) / k
        churn = sum(b[6] + b[7] for b in bucket)
        # First snapshot of the run has no predecessor: everything counts as
        # "added"; skip it so startup doesn't dwarf the churn plot.
        if i == 0 and k > 1:
            churn -= bucket[0][6] + bucket[0][7]
            churn_rate = churn / span_min
        elif i == 0:
            churn_rate = 0.0
        else:
            churn_rate = churn / max(span_min, stride * 2 / 60.0)

        gb = 1024.0 ** 3
        eff = (avg_comm / avg_total * 100.0) if avg_total else 0.0
        points.append({
            'x': round(elapsed_h, 4),
            'vmas': round(avg_vmas, 1),
            'total': round(avg_total / gb, 3),
            'committed': round(avg_comm / gb, 3),
            'reserved': round(avg_resv / gb, 3),
            'churn': round(churn_rate, 2),
            'eff': round(eff, 2),
        })

    # Summary statistics over the full-resolution data
    gb = 1024.0 ** 3
    valid = [(t, s) for t, s in zip(times, snapshots) if t is not None]
    duration_h = ((valid[-1][0] - valid[0][0]).total_seconds() / 3600.0
                  if len(valid) > 1 else 0.0)
    comm = [s[3] for s in snapshots]
    tot = [s[2] for s in snapshots]
    vma = [s[1] for s in snapshots]
    resv = [s[4] for s in snapshots]
    churn_total = sum(s[6] + s[7] for s in snapshots[1:])
    effs = [(c / t * 100.0) for c, t in zip(comm, tot) if t]

    stats = {
        'snapshots': n,
        'durationH': round(duration_h, 2),
        'avgVmas': round(sum(vma) / n, 0),
        'maxVmas': max(vma),
        'maxTotalGB': round(max(tot) / gb, 2),
        'finalCommittedGB': round(comm[-1] / gb, 2),
        'maxCommittedGB': round(max(comm) / gb, 2),
        'committedGrowthGB': round((comm[-1] - comm[0]) / gb, 2),
        'avgReservedGB': round(sum(resv) / n / gb, 2),
        'churnTotal': churn_total,
        'churnPerMin': round(churn_total / max(duration_h * 60.0, 1e-9), 1),
        'avgEff': round(sum(effs) / len(effs), 1) if effs else 0.0,
        'finalEff': round(effs[-1], 1) if effs else 0.0,
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
    ('committed', 'Committed anonymous memory', 'GB',
     'Writable anonymous mappings + [heap] + [stack] — memory the allocator '
     'actually made usable. The closest maps-level proxy for allocator '
     'footprint (grows with RSS demand, shrinks when the allocator returns '
     'memory to the kernel).'),
    ('reserved', 'Reserved address space (PROT_NONE)', 'GB',
     'Inaccessible (---p) anonymous mappings: arena/heap tails the allocator '
     'reserved but has not made usable. Shows how aggressively each '
     'allocator pre-reserves virtual memory.'),
    ('total', 'Total mapped virtual memory', 'GB',
     'Sum of all mapping sizes (≈ VSZ). Committed + reserved + file-backed.'),
    ('vmas', 'Mapping count (VMAs)', 'mappings',
     'Number of lines in /proc/pid/maps. A steadily rising count indicates '
     'virtual address-space fragmentation; each VMA also costs kernel memory '
     'and slows page-fault/munmap paths.'),
    ('churn', 'Mapping churn', 'VMA changes / min',
     'Address ranges added + removed vs the previous snapshot, per minute. '
     'A direct measure of mmap/munmap/mremap activity — memory-related '
     'syscall pressure caused by the allocator.'),
    ('eff', 'Allocation efficiency (committed / total mapped)', '%',
     'Share of mapped address space that is actually usable memory. Low '
     'values mean the allocator holds large reserved/PROT_NONE or unused '
     'ranges relative to what it hands out.'),
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
    <title>MySQL /proc/pid/maps Analysis — THP × Allocator</title>
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
        <h1>MySQL /proc/pid/maps Analysis — THP × Allocator</h1>

        <div class="info-box">
            <strong>Data source:</strong> periodic <code>/proc/&lt;pid&gt;/maps</code>
            snapshots of mysqld from <code>&lt;thp&gt;_&lt;allocator&gt;_mysql_maps_*.log</code>
            in <code>{data_dir}</code>.
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
                <th>Avg VMAs</th>
                <th>Max VMAs</th>
                <th>Max mapped (GB)</th>
                <th>Max committed (GB)</th>
                <th>Final committed (GB)</th>
                <th>Committed growth (GB)</th>
                <th>Avg reserved (GB)</th>
                <th>Churn total (VMAs)</th>
                <th>Churn / min</th>
                <th>Avg eff. (%)</th>
                <th>Final eff. (%)</th>
            </tr></thead>
            <tbody id="statsTableBody"></tbody>
        </table>

        <div class="footer">
            Generated on {generated} | generate_maps_report.py
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
                            beginAtZero: meta.key !== 'eff',
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
                <td>${{s.avgVmas.toLocaleString()}}</td>
                <td>${{s.maxVmas.toLocaleString()}}</td>
                <td>${{s.maxTotalGB}}</td>
                <td>${{s.maxCommittedGB}}</td>
                <td>${{s.finalCommittedGB}}</td>
                <td>${{s.committedGrowthGB}}</td>
                <td>${{s.avgReservedGB}}</td>
                <td>${{s.churnTotal.toLocaleString()}}</td>
                <td>${{s.churnPerMin.toLocaleString()}}</td>
                <td>${{s.avgEff}}</td>
                <td>${{s.finalEff}}</td>
            `;
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html_content)


def main():
    parser = argparse.ArgumentParser(
        description='Generate comparative HTML report from mysql_maps logs '
                    'for each THP/allocator combination.')
    parser.add_argument('data_dir', nargs='?', default='mem-2400',
                        help='Directory with results-* subdirectories '
                             '(default: mem-2400)')
    parser.add_argument('output_file', nargs='?', default='maps_report.html',
                        help='Output HTML file (default: maps_report.html)')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = args.data_dir if os.path.isabs(args.data_dir) \
        else os.path.join(script_dir, args.data_dir)
    output_file = args.output_file if os.path.isabs(args.output_file) \
        else os.path.join(script_dir, args.output_file)

    if not os.path.exists(data_dir):
        print(f"Error: data directory does not exist: {data_dir}")
        sys.exit(1)

    log_files = find_maps_logs(data_dir)
    if not log_files:
        print(f"No *_mysql_maps_*.log files found under {data_dir}")
        sys.exit(1)

    print(f"Found {len(log_files)} result directories with maps logs.\n")

    results = {}
    for dir_name, log_file in log_files.items():
        config = parse_result_dir_name(dir_name)
        size_gb = os.path.getsize(log_file) / 1024**3
        print(f"Processing {config['label']:22s} "
              f"({os.path.basename(log_file)}, {size_gb:.1f} GB)...",
              flush=True)
        snapshots = parse_maps_log(log_file)
        if not snapshots:
            print("  No snapshots parsed, skipping.")
            continue
        points, stats = downsample(snapshots)
        print(f"  {stats['snapshots']} snapshots over {stats['durationH']} h, "
              f"avg VMAs {stats['avgVmas']:.0f}, "
              f"max committed {stats['maxCommittedGB']} GB, "
              f"churn {stats['churnPerMin']}/min")
        results[dir_name] = (config, points, stats)

    if not results:
        print("No data parsed; report not generated.")
        sys.exit(1)

    generate_html_report(results, output_file, os.path.basename(data_dir))
    print(f"\nReport generated: {output_file}")
    print(f"Open in browser: file://{output_file}")


if __name__ == '__main__':
    main()
