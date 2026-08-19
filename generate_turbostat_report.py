#!/usr/bin/env python3
"""
Generate HTML report with CPU frequency graphs from turbostat logs.
Parses *_turbostat_*.log files and plots the all-package summary rows
(Bzy_MHz, IPC, Busy%) over elapsed time for every node.
"""

import os
import re
import glob
import sys
import argparse
from pathlib import Path
import json


# Categorical palette (colorblind-validated, fixed slot order).
# A node always gets the same slot: slot = (node_number - 1) % 8,
# so colors follow the node identity even if some nodes are missing.
PALETTE = [
    '#2a78d6',  # 1 blue
    '#eb6834',  # 2 orange
    '#1baf7a',  # 3 aqua
    '#eda100',  # 4 yellow
    '#e87ba4',  # 5 magenta
    '#008300',  # 6 green
    '#4a3aa7',  # 7 violet
    '#e34948',  # 8 red
]

# Samples to skip for "steady state" stats: turbostat samples every 30 s,
# the all-core turbo settles within the first hour => skip 120 samples.
STEADY_SKIP = 120


def parse_turbostat_log(log_file):
    """
    Parse a turbostat log file and extract the all-package summary rows
    (Package == Core == CPU == '-').
    Returns a list of (elapsed_seconds, bzy_mhz, busy_pct, ipc) tuples.
    """
    data_points = []
    start_time = None

    with open(log_file, 'r') as f:
        for line in f:
            fields = line.split()
            # Summary row: Time_Of_Day_Seconds  -  -  -  Avg_MHz  Busy%  Bzy_MHz  TSC_MHz  IPC ...
            if len(fields) < 9 or fields[1] != '-' or fields[2] != '-' or fields[3] != '-':
                continue
            try:
                timestamp = float(fields[0])
                busy_pct = float(fields[5])
                bzy_mhz = float(fields[6])
                ipc = float(fields[8])
            except ValueError:
                continue

            if start_time is None:
                start_time = timestamp
            data_points.append((timestamp - start_time, bzy_mhz, busy_pct, ipc))

    return data_points


def find_turbostat_logs(data_dir):
    """
    Find all *_turbostat_*.log files in subdirectories.
    Returns a dict: {result_dir_name: log_file_path}
    """
    results = {}

    if not os.path.exists(data_dir):
        return results

    for entry in os.listdir(data_dir):
        result_dir = os.path.join(data_dir, entry)

        if not os.path.isdir(result_dir):
            continue

        pattern = os.path.join(result_dir, '*_turbostat_*.log')
        log_files = glob.glob(pattern)

        if log_files:
            results[entry] = log_files[0]

    return results


def detect_node_numbers(dir_names):
    """
    Find the node number in each directory name: the number that changes
    from directory to directory.

    Directory naming differs between result sets (results-ps-8.4.10-1-...-glibc1-...,
    results-ref5-...-glibc5-..., results-refa8-...-glibc-...), so no fixed token
    can be relied on. Instead all numbers are extracted from every name in order,
    and the first position whose value varies across directories is taken as the
    node number (constant positions are version/size tokens like 8.4.10 or 150G).

    Returns {dir_name: node_number}; falls back to the glibc<N> token, then the
    first number in the name, when variation cannot be established (e.g. a
    single directory).
    """
    numbers = {name: re.findall(r'\d+', name) for name in dir_names}

    counts = {len(v) for v in numbers.values()}
    if len(dir_names) > 1 and len(counts) == 1:
        for idx in range(counts.pop()):
            if len({numbers[name][idx] for name in dir_names}) > 1:
                return {name: int(numbers[name][idx]) for name in dir_names}

    result = {}
    for name in dir_names:
        match = re.search(r'glibc(\d+)', name)
        if match:
            result[name] = int(match.group(1))
        elif numbers[name]:
            result[name] = int(numbers[name][0])
        else:
            result[name] = None
    return result


def moving_average(data, value_idx, window_size=25):
    """
    Centered moving average over one column of the data tuples.
    Returns a list of {'x': elapsed, 'y': avg} points.
    """
    half_window = window_size // 2
    averaged = []
    for i in range(len(data)):
        start_idx = max(0, i - half_window)
        end_idx = min(len(data), i + half_window + 1)
        window = data[start_idx:end_idx]
        avg = sum(point[value_idx] for point in window) / len(window)
        averaged.append({'x': round(data[i][0], 1), 'y': round(avg, 2)})
    return averaged


def steady_state_stats(data):
    """
    Compute steady-state statistics (skipping the warm-up samples).
    Returns a dict with bzy/ipc/busy aggregates.
    """
    steady = data[STEADY_SKIP:] if len(data) > STEADY_SKIP else data
    bzy_values = [bzy for _, bzy, _, _ in steady]
    return {
        'avg_bzy': sum(bzy_values) / len(bzy_values),
        'min_bzy': min(bzy_values),
        'max_bzy': max(bzy_values),
        'avg_ipc': sum(ipc for _, _, _, ipc in steady) / len(steady),
        'avg_busy': sum(busy for _, _, busy, _ in steady) / len(steady),
        'samples': len(data),
    }


def generate_html_report(node_results, output_file):
    """
    Generate an HTML report with interactive turbostat graphs using Chart.js.
    node_results: {node_number: {'dir_name': ..., 'data': [(elapsed, bzy, busy, ipc), ...]}}
    """
    freq_datasets = []
    ipc_datasets = []
    stats_rows = []

    for node in sorted(node_results.keys()):
        run = node_results[node]
        data = run['data']
        if not data:
            continue

        color = PALETTE[(node - 1) % len(PALETTE)]
        label = f"Node-{node}"

        # Raw Bzy_MHz line (semi-transparent)
        freq_datasets.append({
            'label': f"{label} (raw)",
            'data': [{'x': round(elapsed, 1), 'y': bzy} for elapsed, bzy, _, _ in data],
            'borderColor': color + '40',
            'backgroundColor': 'transparent',
            'tension': 0.1,
            'pointRadius': 0,
            'borderWidth': 2,
        })

        # Moving average Bzy_MHz line (solid)
        freq_datasets.append({
            'label': label,
            'data': moving_average(data, 1),
            'borderColor': color,
            'backgroundColor': 'transparent',
            'borderWidth': 2,
            'pointRadius': 0,
            'tension': 0.3,
        })

        # IPC moving average line
        ipc_datasets.append({
            'label': label,
            'data': moving_average(data, 3),
            'borderColor': color,
            'backgroundColor': 'transparent',
            'borderWidth': 2,
            'pointRadius': 0,
            'tension': 0.3,
        })

        stats = steady_state_stats(data)
        stats_rows.append({
            'label': label,
            'color': color,
            'dir_name': run['dir_name'],
            'avg_bzy': round(stats['avg_bzy'], 1),
            'min_bzy': round(stats['min_bzy']),
            'max_bzy': round(stats['max_bzy']),
            'avg_ipc': round(stats['avg_ipc'], 4),
            'avg_busy': round(stats['avg_busy'], 2),
            'samples': stats['samples'],
        })

    # Frequency deltas vs the slowest node (steady state)
    baseline_bzy = min(row['avg_bzy'] for row in stats_rows)
    for row in stats_rows:
        row['delta_pct'] = round((row['avg_bzy'] / baseline_bzy - 1) * 100, 2)

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Turbostat CPU Frequency Analysis</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 890px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #555;
            margin-top: 30px;
        }}
        .chart-container {{
            position: relative;
            height: 600px;
            margin: 30px 0;
        }}
        .stats-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        .stats-table th,
        .stats-table td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        .stats-table th {{
            background-color: #4CAF50;
            color: white;
            font-weight: bold;
        }}
        .stats-table tr:hover {{
            background-color: #f5f5f5;
        }}
        .info-box {{
            background-color: #e7f3ff;
            border-left: 4px solid #2196F3;
            padding: 15px;
            margin: 20px 0;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #666;
            font-size: 14px;
        }}
        .controls {{
            margin: 20px 0;
            padding: 15px;
            background-color: #ffffff;
            border-radius: 5px;
            border: 1px solid #ddd;
        }}
        .controls h3 {{
            margin-top: 0;
            color: #555;
        }}
        .checkbox-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 10px;
            margin-top: 10px;
        }}
        .checkbox-item {{
            display: flex;
            align-items: center;
            padding: 5px;
        }}
        .checkbox-item input[type="checkbox"] {{
            margin-right: 8px;
            cursor: pointer;
        }}
        .checkbox-item label {{
            cursor: pointer;
            user-select: none;
            display: flex;
            align-items: center;
        }}
        .color-indicator {{
            display: inline-block;
            width: 20px;
            height: 20px;
            margin-right: 8px;
            border: 1px solid #ccc;
            border-radius: 3px;
        }}
        .zoom-button {{
            margin-top: 15px;
            padding: 10px 20px;
            background-color: #4CAF50;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
        }}
        .zoom-button:hover {{
            background-color: #45a049;
        }}
        .reset-zoom-button {{
            margin-left: 10px;
            padding: 10px 20px;
            background-color: #f44336;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            font-weight: bold;
        }}
        .reset-zoom-button:hover {{
            background-color: #da190b;
        }}
        .zoom-info {{
            margin-top: 10px;
            padding: 10px;
            background-color: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 5px;
            font-size: 13px;
            color: #856404;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Turbostat CPU Frequency Analysis</h1>

        <div class="info-box">
            <strong>Data Source:</strong> all-package summary rows from *_turbostat_*.log files (30 s samples)
            <br>
            <strong>Bzy_MHz:</strong> average frequency while busy; <strong>IPC:</strong> instructions per cycle
            <br>
            <strong>Steady state:</strong> statistics skip the first {STEADY_SKIP} samples (1 h all-core turbo warm-up)
        </div>

        <div class="controls">
            <h3>Graph Controls</h3>
            <div class="checkbox-grid" id="checkboxGrid">
                <!-- Checkboxes will be generated here -->
            </div>
            <button id="resetZoomButton" class="reset-zoom-button" style="margin-left: 0; margin-top: 15px;">Reset Zoom</button>
            <button id="toggleRawButton" class="zoom-button" style="margin-left: 10px;">Hide Raw Data (Average Only)</button>
            <div class="zoom-info">
                💡 <strong>Tip:</strong> Click and drag on a graph to zoom into a specific area. Use the "Reset Zoom" button to return to full view.
            </div>
        </div>

        <h2>Sustained CPU Frequency (Bzy_MHz) Over Time</h2>
        <div class="chart-container">
            <canvas id="freqChart"></canvas>
        </div>

        <h2>IPC Over Time (25-sample moving average)</h2>
        <div class="chart-container">
            <canvas id="ipcChart"></canvas>
        </div>

        <h2>Steady-State Summary</h2>
        <table class="stats-table">
            <thead>
                <tr>
                    <th>Node</th>
                    <th>Avg Bzy_MHz</th>
                    <th>vs slowest</th>
                    <th>Min</th>
                    <th>Max</th>
                    <th>Avg IPC</th>
                    <th>Avg Busy%</th>
                    <th>Samples</th>
                </tr>
            </thead>
            <tbody id="statsTableBody">
            </tbody>
        </table>

        <div class="footer">
            Generated from turbostat logs | CPU Frequency Analysis Tool
        </div>
    </div>

    <script>
        // Chart data
        const freqDatasets = {json.dumps(freq_datasets)};
        const ipcDatasets = {json.dumps(ipc_datasets)};
        const statsRows = {json.dumps(stats_rows)};

        function hoursTick(value) {{
            return (value / 3600).toFixed(0) + 'h';
        }}

        const zoomOptions = {{
            pan: {{
                enabled: true,
                mode: 'xy',
            }},
            zoom: {{
                drag: {{
                    enabled: true,
                    backgroundColor: 'rgba(54, 162, 235, 0.2)',
                    borderColor: 'rgb(54, 162, 235)',
                    borderWidth: 2,
                }},
                mode: 'xy',
            }}
        }};

        // Create the frequency chart
        const freqCtx = document.getElementById('freqChart').getContext('2d');
        const freqChart = new Chart(freqCtx, {{
            type: 'line',
            data: {{
                datasets: freqDatasets
            }},
            options: {{
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    title: {{
                        display: true,
                        text: 'CPU Frequency (Bzy_MHz) vs Elapsed Time',
                        font: {{
                            size: 18
                        }}
                    }},
                    legend: {{
                        display: false
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                return context.dataset.label + ': ' + context.parsed.y.toFixed(0) + ' MHz';
                            }}
                        }}
                    }},
                    zoom: zoomOptions
                }},
                scales: {{
                    x: {{
                        type: 'linear',
                        title: {{
                            display: true,
                            text: 'Elapsed Time (hours)'
                        }},
                        ticks: {{
                            callback: hoursTick
                        }}
                    }},
                    y: {{
                        title: {{
                            display: true,
                            text: 'Bzy_MHz'
                        }}
                    }}
                }}
            }}
        }});

        // Create the IPC chart
        const ipcCtx = document.getElementById('ipcChart').getContext('2d');
        const ipcChart = new Chart(ipcCtx, {{
            type: 'line',
            data: {{
                datasets: ipcDatasets
            }},
            options: {{
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    title: {{
                        display: true,
                        text: 'IPC vs Elapsed Time',
                        font: {{
                            size: 18
                        }}
                    }},
                    legend: {{
                        display: false
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                return context.dataset.label + ': ' + context.parsed.y.toFixed(4) + ' IPC';
                            }}
                        }}
                    }},
                    zoom: zoomOptions
                }},
                scales: {{
                    x: {{
                        type: 'linear',
                        title: {{
                            display: true,
                            text: 'Elapsed Time (hours)'
                        }},
                        ticks: {{
                            callback: hoursTick
                        }}
                    }},
                    y: {{
                        title: {{
                            display: true,
                            text: 'IPC'
                        }}
                    }}
                }}
            }}
        }});

        // Populate the stats table (sorted by average frequency descending)
        const tbody = document.getElementById('statsTableBody');
        statsRows.slice().sort((a, b) => b.avg_bzy - a.avg_bzy).forEach(stat => {{
            const row = tbody.insertRow();
            row.innerHTML = `
                <td><span class="color-indicator" style="background-color: ${{stat.color}}"></span>${{stat.label}}</td>
                <td>${{stat.avg_bzy.toFixed(1)}}</td>
                <td>+${{stat.delta_pct.toFixed(2)}}%</td>
                <td>${{stat.min_bzy}}</td>
                <td>${{stat.max_bzy}}</td>
                <td>${{stat.avg_ipc.toFixed(4)}}</td>
                <td>${{stat.avg_busy.toFixed(2)}}</td>
                <td>${{stat.samples}}</td>
            `;
        }});

        // Generate checkboxes for node visibility control.
        // freqDatasets holds pairs (raw + average) per node; ipcDatasets one per node.
        const checkboxGrid = document.getElementById('checkboxGrid');

        const configs = [];
        for (let i = 0; i < freqDatasets.length; i += 2) {{
            configs.push({{
                rawIndex: i,
                avgIndex: i + 1,
                ipcIndex: i / 2,
                label: freqDatasets[i + 1].label,
            }});
        }}

        let showRawData = true;

        configs.forEach((config, idx) => {{
            const div = document.createElement('div');
            div.className = 'checkbox-item';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.id = `graph-${{idx}}`;
            checkbox.checked = true;
            checkbox.addEventListener('change', function() {{
                freqChart.data.datasets[config.rawIndex].hidden = !this.checked || !showRawData;
                freqChart.data.datasets[config.avgIndex].hidden = !this.checked;
                ipcChart.data.datasets[config.ipcIndex].hidden = !this.checked;
                freqChart.update();
                ipcChart.update();
            }});

            const label = document.createElement('label');
            label.htmlFor = `graph-${{idx}}`;

            const colorIndicator = document.createElement('span');
            colorIndicator.className = 'color-indicator';
            colorIndicator.style.backgroundColor = freqDatasets[config.avgIndex].borderColor;

            const textSpan = document.createElement('span');
            textSpan.textContent = config.label;

            label.appendChild(colorIndicator);
            label.appendChild(textSpan);

            div.appendChild(checkbox);
            div.appendChild(label);
            checkboxGrid.appendChild(div);
        }});

        // Toggle raw (per-sample) frequency lines, leaving only the average lines
        const toggleRawButton = document.getElementById('toggleRawButton');
        toggleRawButton.addEventListener('click', function() {{
            showRawData = !showRawData;
            configs.forEach((config, idx) => {{
                const checkbox = document.getElementById(`graph-${{idx}}`);
                const isChecked = checkbox ? checkbox.checked : true;
                freqChart.data.datasets[config.rawIndex].hidden = !isChecked || !showRawData;
            }});
            toggleRawButton.textContent = showRawData ? 'Hide Raw Data (Average Only)' : 'Show Raw Data';
            freqChart.update();
        }});

        // Reset zoom for both charts
        const resetZoomButton = document.getElementById('resetZoomButton');
        resetZoomButton.addEventListener('click', function() {{
            freqChart.resetZoom();
            ipcChart.resetZoom();
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html_content)


def main():
    parser = argparse.ArgumentParser(
        description='Generate HTML report with CPU frequency graphs from turbostat logs.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s mem-cpu turbostat_report.html
  %(prog)s /path/to/results my_report.html
  %(prog)s                                    # Uses default: mem-cpu/ and turbostat_report.html
        '''
    )
    parser.add_argument(
        'data_dir',
        nargs='?',
        default='mem-cpu',
        help='Directory containing result subdirectories (default: mem-cpu)'
    )
    parser.add_argument(
        'output_file',
        nargs='?',
        default='turbostat_report.html',
        help='Output HTML file name (default: turbostat_report.html)'
    )

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isabs(args.data_dir):
        data_dir = os.path.join(script_dir, args.data_dir)
    else:
        data_dir = args.data_dir

    if not os.path.isabs(args.output_file):
        output_file = os.path.join(script_dir, args.output_file)
    else:
        output_file = args.output_file

    print(f"Data directory: {data_dir}")
    print(f"Output file: {output_file}")
    print()

    if not os.path.exists(data_dir):
        print(f"Error: Data directory does not exist: {data_dir}")
        sys.exit(1)

    print("Searching for turbostat log files...")
    log_files = find_turbostat_logs(data_dir)

    if not log_files:
        print("No turbostat log files found in subdirectories.")
        return

    print(f"Found {len(log_files)} result directories with turbostat logs.")

    node_map = detect_node_numbers(sorted(log_files.keys()))

    node_results = {}

    for dir_name, log_file in sorted(log_files.items()):
        node = node_map.get(dir_name)
        if node is None:
            print(f"\nSkipping {dir_name} (no number found in directory name)")
            continue
        if node in node_results:
            print(f"\nSkipping {dir_name} (node {node} already taken by "
                  f"{node_results[node]['dir_name']})")
            continue

        print(f"\nProcessing Node-{node}: {dir_name}")
        print(f"  Log file: {os.path.basename(log_file)}")

        data = parse_turbostat_log(log_file)
        print(f"  Found {len(data)} summary samples")

        if len(data) <= STEADY_SKIP:
            print(f"  Warning: fewer samples than the steady-state skip ({STEADY_SKIP}); using all samples for stats")

        if data:
            stats = steady_state_stats(data)
            print(f"  Steady-state Bzy_MHz: avg {stats['avg_bzy']:.1f}, "
                  f"min {stats['min_bzy']:.0f}, max {stats['max_bzy']:.0f}, "
                  f"IPC {stats['avg_ipc']:.4f}, Busy% {stats['avg_busy']:.2f}")
            node_results[node] = {'dir_name': dir_name, 'data': data}

    if not node_results:
        print("\nNo turbostat data to generate report.")
        return

    print(f"\nGenerating HTML report: {output_file}")
    generate_html_report(node_results, output_file)
    print(f"Report generated successfully!")
    print(f"\nOpen the report in your browser:")
    print(f"  file://{output_file}")


if __name__ == '__main__':
    main()
