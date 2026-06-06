#!/usr/bin/env python3
"""
Generate HTML report with QPS graphs from HammerDB benchmark results.
Parses *_global_status_*.log files and calculates QPS from Questions and Uptime.
"""

import os
import re
import glob
from pathlib import Path
from collections import defaultdict
import json


def parse_global_status_log(log_file):
    """
    Parse a global status log file and extract snapshots with Uptime and Questions.
    Returns a list of (timestamp, uptime, questions) tuples.
    """
    snapshots = []
    current_timestamp = None
    current_data = {}

    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()

            # Match timestamp lines like "=== 2026-06-05 10:15:50 ==="
            timestamp_match = re.match(r'^===\s+([\d-]+\s+[\d:]+)\s+===$', line)
            if timestamp_match:
                # Save previous snapshot if complete
                if current_timestamp and 'Uptime' in current_data and 'Questions' in current_data:
                    snapshots.append((
                        current_timestamp,
                        current_data['Uptime'],
                        current_data['Questions']
                    ))

                current_timestamp = timestamp_match.group(1)
                current_data = {}
                continue

            # Match data lines (tab-separated)
            if '\t' in line:
                parts = line.split('\t')
                if len(parts) == 2:
                    var_name, var_value = parts
                    if var_name == 'Uptime':
                        try:
                            current_data['Uptime'] = int(var_value)
                        except ValueError:
                            pass
                    elif var_name == 'Questions':
                        try:
                            current_data['Questions'] = int(var_value)
                        except ValueError:
                            pass

        # Save last snapshot
        if current_timestamp and 'Uptime' in current_data and 'Questions' in current_data:
            snapshots.append((
                current_timestamp,
                current_data['Uptime'],
                current_data['Questions']
            ))

    return snapshots


def calculate_qps(snapshots):
    """
    Calculate QPS from snapshots using the formula:
    QPS_N = (q_N - q_N-1) / (u_N - u_N-1)

    Returns a list of (timestamp, uptime, qps) tuples.
    """
    if len(snapshots) < 2:
        return []

    qps_data = []

    for i in range(1, len(snapshots)):
        timestamp_curr, uptime_curr, questions_curr = snapshots[i]
        timestamp_prev, uptime_prev, questions_prev = snapshots[i-1]

        time_delta = uptime_curr - uptime_prev
        questions_delta = questions_curr - questions_prev

        if time_delta > 0:
            qps = questions_delta / time_delta
            qps_data.append((timestamp_curr, uptime_curr, qps))

    return qps_data


def find_global_status_logs(base_dir):
    """
    Find all *_global_status_*.log files in mem-results subdirectories.
    Returns a dict: {result_dir_name: log_file_path}
    """
    results = {}
    mem_results_dir = os.path.join(base_dir, 'mem-results')

    if not os.path.exists(mem_results_dir):
        return results

    # Find all results-* directories
    for entry in os.listdir(mem_results_dir):
        result_dir = os.path.join(mem_results_dir, entry)

        # Skip non-directories and .tar.gz files
        if not os.path.isdir(result_dir):
            continue

        # Find global status log files
        pattern = os.path.join(result_dir, '*_global_status_*.log')
        log_files = glob.glob(pattern)

        if log_files:
            # Use the first log file found
            results[entry] = log_files[0]

    return results


def find_rss_memory_logs(base_dir):
    """
    Find all *_rss_memory_*.log files in mem-results subdirectories.
    Returns a dict: {result_dir_name: log_file_path}
    """
    results = {}
    mem_results_dir = os.path.join(base_dir, 'mem-results')

    if not os.path.exists(mem_results_dir):
        return results

    # Find all results-* directories
    for entry in os.listdir(mem_results_dir):
        result_dir = os.path.join(mem_results_dir, entry)

        # Skip non-directories and .tar.gz files
        if not os.path.isdir(result_dir):
            continue

        # Find RSS memory log files
        pattern = os.path.join(result_dir, '*_rss_memory_*.log')
        log_files = glob.glob(pattern)

        if log_files:
            # Use the first log file found
            results[entry] = log_files[0]

    return results


def parse_rss_memory_log(log_file):
    """
    Parse RSS memory log file and extract mysqld RSS data (every minute).
    Returns a list of (timestamp, elapsed_seconds, rss_mb) tuples.
    """
    data_points = []
    start_time = None

    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip comments
            if line.startswith('#') or not line:
                continue

            # Parse data line: Timestamp, mysqld_PID, mysqld_RSS_KB, hammerdbcli_PID, hammerdbcli_RSS_KB
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 3:
                try:
                    timestamp_str = parts[0]
                    mysqld_rss_kb = int(parts[2])

                    # Parse timestamp
                    from datetime import datetime
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

                    if start_time is None:
                        start_time = timestamp

                    # Calculate elapsed seconds
                    elapsed = (timestamp - start_time).total_seconds()

                    # Convert KB to MB
                    rss_mb = mysqld_rss_kb / 1024

                    data_points.append((timestamp_str, elapsed, rss_mb))
                except (ValueError, IndexError):
                    continue

    # Sample every minute (every 60 seconds)
    sampled_data = []
    for i, (timestamp_str, elapsed, rss_mb) in enumerate(data_points):
        if i == 0 or int(elapsed) % 60 == 0:
            sampled_data.append((timestamp_str, elapsed, rss_mb))

    return sampled_data


def parse_result_dir_name(dir_name):
    """
    Parse result directory name to extract configuration.
    Expected format: results-<suffix>-<thp>-<allocator>-<buffer_pool>
    Example: results-ps-8.4.9-9-CUSTOM113-nothp-glibc-150G
    """
    parts = dir_name.split('-')

    # Try to extract thp, allocator from the end
    if len(parts) >= 3:
        # Last part should be buffer pool (e.g., 150G)
        buffer_pool = parts[-1]
        # Second to last should be allocator
        allocator = parts[-2]
        # Third to last should be thp setting
        thp = parts[-3]
        # Everything else is the suffix
        suffix = '-'.join(parts[1:-3])

        return {
            'suffix': suffix,
            'thp': thp,
            'allocator': allocator,
            'buffer_pool': buffer_pool,
            'label': f"{thp}-{allocator}"
        }

    return {
        'suffix': dir_name,
        'thp': 'unknown',
        'allocator': 'unknown',
        'buffer_pool': 'unknown',
        'label': dir_name
    }


def generate_html_report(qps_results, rss_results, output_file):
    """
    Generate an HTML report with interactive QPS graphs using Chart.js.
    """

    # Group results by configuration
    grouped_data = defaultdict(list)

    for dir_name, qps_data in qps_results.items():
        config = parse_result_dir_name(dir_name)
        grouped_data[config['label']].append({
            'dir_name': dir_name,
            'config': config,
            'qps_data': qps_data
        })

    # Prepare data for QPS chart
    chart_datasets = []

    # Prepare data for RSS chart
    rss_datasets = []

    colors = [
        'rgb(255, 99, 132)',   # red
        'rgb(54, 162, 235)',   # blue
        'rgb(255, 205, 86)',   # yellow
        'rgb(75, 192, 192)',   # green
        'rgb(153, 102, 255)',  # purple
        'rgb(255, 159, 64)',   # orange
        'rgb(201, 203, 207)',  # grey
        'rgb(83, 102, 255)',   # indigo
    ]

    color_idx = 0
    for label, runs in sorted(grouped_data.items()):
        for run in runs:
            qps_data = run['qps_data']
            if not qps_data:
                continue

            color = colors[color_idx % len(colors)]
            color_idx += 1

            # Main data line (semi-transparent)
            dataset = {
                'label': f"{label} ({run['config']['suffix']})",
                'data': [{'x': uptime, 'y': round(qps, 2)} for _, uptime, qps in qps_data],
                'borderColor': color.replace('rgb', 'rgba').replace(')', ', 0.25)'),
                'backgroundColor': color.replace('rgb', 'rgba').replace(')', ', 0.05)'),
                'tension': 0.1,
                'pointRadius': 0,
                'borderWidth': 2,
            }
            chart_datasets.append(dataset)

            # Calculate centered moving average (25-point window)
            window_size = 25
            half_window = window_size // 2
            moving_avg_data = []
            for i in range(len(qps_data)):
                # Center the window around the current point
                start_idx = max(0, i - half_window)
                end_idx = min(len(qps_data), i + half_window + 1)
                window = qps_data[start_idx:end_idx]
                avg_qps = sum(qps for _, _, qps in window) / len(window)
                timestamp, uptime, _ = qps_data[i]
                moving_avg_data.append({'x': uptime, 'y': round(avg_qps, 2)})

            avg_dataset = {
                'label': f"{label} avg (25pt)",
                'data': moving_avg_data,
                'borderColor': color,
                'backgroundColor': 'transparent',
                'borderWidth': 3,
                'pointRadius': 0,
                'tension': 0.3,
            }
            chart_datasets.append(avg_dataset)

    # Prepare RSS memory datasets
    color_idx = 0
    for dir_name, rss_data in sorted(rss_results.items()):
        if not rss_data:
            continue

        config = parse_result_dir_name(dir_name)
        color = colors[color_idx % len(colors)]
        color_idx += 1

        dataset = {
            'label': f"{config['label']} ({config['suffix']})",
            'data': [{'x': elapsed, 'y': round(rss_mb, 2)} for _, elapsed, rss_mb in rss_data],
            'borderColor': color,
            'backgroundColor': color.replace('rgb', 'rgba').replace(')', ', 0.1)'),
            'tension': 0.3,
            'pointRadius': 0,
            'borderWidth': 2,
        }
        rss_datasets.append(dataset)

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HammerDB QPS Analysis</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1400px;
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
            height: 1200px;
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
            background-color: #f9f9f9;
            border-radius: 5px;
            border: 1px solid #ddd;
        }}
        .controls h3 {{
            margin-top: 0;
            color: #555;
        }}
        .checkbox-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
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
        .zoom-button:active {{
            background-color: #3d8b40;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>HammerDB TPC-C Benchmark - QPS Analysis</h1>

        <div class="info-box">
            <strong>QPS Calculation:</strong> QPS<sub>N</sub> = (Questions<sub>N</sub> - Questions<sub>N-1</sub>) / (Uptime<sub>N</sub> - Uptime<sub>N-1</sub>)
            <br>
            <strong>Data Source:</strong> MySQL SHOW GLOBAL STATUS snapshots from *_global_status_*.log files
        </div>

        <div class="controls">
            <h3>Graph Controls</h3>
            <div class="checkbox-grid" id="checkboxGrid">
                <!-- Checkboxes will be generated here -->
            </div>
            <button id="zoomButton" class="zoom-button">Zoom In (Start at 40k)</button>
        </div>

        <h2>QPS Over Time (by Uptime)</h2>
        <div class="chart-container">
            <canvas id="qpsChart"></canvas>
        </div>

        <h2>MySQL RSS Memory Usage Over Time</h2>
        <button id="rssZoomButton" class="zoom-button" style="margin-bottom: 15px;">Zoom In (Start at 160k MB)</button>
        <div class="chart-container">
            <canvas id="rssChart"></canvas>
        </div>

        <h2>Summary Statistics</h2>
        <table class="stats-table">
            <thead>
                <tr>
                    <th>Configuration</th>
                    <th>THP</th>
                    <th>Allocator</th>
                    <th>Avg QPS</th>
                    <th>Max QPS</th>
                    <th>Min QPS</th>
                    <th>Snapshots</th>
                </tr>
            </thead>
            <tbody id="statsTableBody">
            </tbody>
        </table>

        <div class="footer">
            Generated on {Path(output_file).name} | HammerDB Benchmark Analysis Tool
        </div>
    </div>

    <script>
        // Chart data
        const datasets = {json.dumps(chart_datasets, indent=8)};
        const rssDatasets = {json.dumps(rss_datasets, indent=8)};

        // Create the QPS chart
        const ctx = document.getElementById('qpsChart').getContext('2d');
        const qpsChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                datasets: datasets
            }},
            options: {{
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    title: {{
                        display: true,
                        text: 'Queries Per Second (QPS) vs Uptime',
                        font: {{
                            size: 18
                        }}
                    }},
                    legend: {{
                        display: true,
                        position: 'top',
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                return context.dataset.label + ': ' + context.parsed.y.toFixed(2) + ' QPS';
                            }}
                        }}
                    }}
                }},
                scales: {{
                    x: {{
                        type: 'linear',
                        title: {{
                            display: true,
                            text: 'Uptime (seconds)'
                        }},
                        ticks: {{
                            callback: function(value) {{
                                // Convert seconds to minutes for display
                                return Math.floor(value / 60) + 'm';
                            }}
                        }}
                    }},
                    y: {{
                        title: {{
                            display: true,
                            text: 'QPS (Queries Per Second)'
                        }},
                        beginAtZero: true,
                        min: 0
                    }}
                }}
            }}
        }});

        // Create the RSS memory chart
        const rssCtx = document.getElementById('rssChart').getContext('2d');
        const rssChart = new Chart(rssCtx, {{
            type: 'line',
            data: {{
                datasets: rssDatasets
            }},
            options: {{
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    title: {{
                        display: true,
                        text: 'MySQL RSS Memory Usage (sampled every minute)',
                        font: {{
                            size: 18
                        }}
                    }},
                    legend: {{
                        display: true,
                        position: 'top',
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                return context.dataset.label + ': ' + context.parsed.y.toFixed(2) + ' MB';
                            }}
                        }}
                    }}
                }},
                scales: {{
                    x: {{
                        type: 'linear',
                        title: {{
                            display: true,
                            text: 'Elapsed Time (seconds)'
                        }},
                        ticks: {{
                            callback: function(value) {{
                                // Convert seconds to minutes for display
                                return Math.floor(value / 60) + 'm';
                            }}
                        }}
                    }},
                    y: {{
                        title: {{
                            display: true,
                            text: 'RSS Memory (MB)'
                        }},
                        beginAtZero: true,
                        min: 0
                    }}
                }}
            }}
        }});

        // Calculate and populate statistics table
        const statsData = [];
        datasets.forEach(dataset => {{
            const qpsValues = dataset.data.map(d => d.y);
            const avgQps = qpsValues.reduce((a, b) => a + b, 0) / qpsValues.length;
            const maxQps = Math.max(...qpsValues);
            const minQps = Math.min(...qpsValues);

            // Parse label to extract configuration
            const labelMatch = dataset.label.match(/^(\\w+)-(\\w+)/);
            const thp = labelMatch ? labelMatch[1] : 'unknown';
            const allocator = labelMatch ? labelMatch[2] : 'unknown';

            statsData.push({{
                label: dataset.label,
                thp: thp,
                allocator: allocator,
                avgQps: avgQps.toFixed(2),
                maxQps: maxQps.toFixed(2),
                minQps: minQps.toFixed(2),
                snapshots: qpsValues.length
            }});
        }});

        // Sort by average QPS descending
        statsData.sort((a, b) => parseFloat(b.avgQps) - parseFloat(a.avgQps));

        // Populate table
        const tbody = document.getElementById('statsTableBody');
        statsData.forEach(stat => {{
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${{stat.label}}</td>
                <td>${{stat.thp}}</td>
                <td>${{stat.allocator}}</td>
                <td>${{stat.avgQps}}</td>
                <td>${{stat.maxQps}}</td>
                <td>${{stat.minQps}}</td>
                <td>${{stat.snapshots}}</td>
            `;
        }});

        // Generate checkboxes for graph visibility control
        const checkboxGrid = document.getElementById('checkboxGrid');

        // Group datasets by configuration (every 2 datasets: raw data + average)
        const configs = [];
        for (let i = 0; i < datasets.length; i += 2) {{
            const dataDataset = datasets[i];
            const avgDataset = datasets[i + 1];
            configs.push({{
                dataIndex: i,
                avgIndex: i + 1,
                label: dataDataset.label,
                enabled: true
            }});
        }}

        configs.forEach((config, idx) => {{
            const div = document.createElement('div');
            div.className = 'checkbox-item';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.id = `graph-${{idx}}`;
            checkbox.checked = true;
            checkbox.addEventListener('change', function() {{
                // Toggle visibility of both raw data and average line
                qpsChart.data.datasets[config.dataIndex].hidden = !this.checked;
                qpsChart.data.datasets[config.avgIndex].hidden = !this.checked;
                qpsChart.update();
            }});

            const label = document.createElement('label');
            label.htmlFor = `graph-${{idx}}`;

            // Get the color from the average dataset (solid color)
            const color = datasets[config.avgIndex].borderColor;

            // Create color indicator
            const colorIndicator = document.createElement('span');
            colorIndicator.className = 'color-indicator';
            colorIndicator.style.backgroundColor = color;

            // Create text span
            const textSpan = document.createElement('span');
            textSpan.textContent = config.label;

            label.appendChild(colorIndicator);
            label.appendChild(textSpan);

            div.appendChild(checkbox);
            div.appendChild(label);
            checkboxGrid.appendChild(div);
        }});

        // Zoom button functionality for QPS chart
        let isZoomedIn = false;
        const zoomButton = document.getElementById('zoomButton');
        zoomButton.addEventListener('click', function() {{
            if (isZoomedIn) {{
                // Reset to full view
                qpsChart.options.scales.y.min = 0;
                qpsChart.options.scales.y.beginAtZero = true;
                zoomButton.textContent = 'Zoom In (Start at 40k)';
                isZoomedIn = false;
            }} else {{
                // Zoom in
                qpsChart.options.scales.y.min = 40000;
                qpsChart.options.scales.y.beginAtZero = false;
                zoomButton.textContent = 'Reset Zoom';
                isZoomedIn = true;
            }}
            qpsChart.update();
        }});

        // Zoom button functionality for RSS chart
        let isRssZoomedIn = false;
        const rssZoomButton = document.getElementById('rssZoomButton');
        rssZoomButton.addEventListener('click', function() {{
            if (isRssZoomedIn) {{
                // Reset to full view
                rssChart.options.scales.y.min = 0;
                rssChart.options.scales.y.beginAtZero = true;
                rssZoomButton.textContent = 'Zoom In (Start at 160k MB)';
                isRssZoomedIn = false;
            }} else {{
                // Zoom in
                rssChart.options.scales.y.min = 160000;
                rssChart.options.scales.y.beginAtZero = false;
                rssZoomButton.textContent = 'Reset Zoom';
                isRssZoomedIn = true;
            }}
            rssChart.update();
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html_content)


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    print("Searching for global status log files...")
    log_files = find_global_status_logs(base_dir)

    if not log_files:
        print("No global status log files found in mem-results subdirectories.")
        return

    print(f"Found {len(log_files)} result directories with global status logs.")

    qps_results = {}

    for dir_name, log_file in sorted(log_files.items()):
        print(f"\nProcessing QPS: {dir_name}")
        print(f"  Log file: {os.path.basename(log_file)}")

        snapshots = parse_global_status_log(log_file)
        print(f"  Found {len(snapshots)} snapshots")

        if len(snapshots) < 2:
            print(f"  Skipping (need at least 2 snapshots)")
            continue

        qps_data = calculate_qps(snapshots)
        print(f"  Calculated {len(qps_data)} QPS data points")

        if qps_data:
            avg_qps = sum(qps for _, _, qps in qps_data) / len(qps_data)
            max_qps = max(qps for _, _, qps in qps_data)
            print(f"  Avg QPS: {avg_qps:.2f}, Max QPS: {max_qps:.2f}")

            qps_results[dir_name] = qps_data

    if not qps_results:
        print("\nNo QPS data to generate report.")
        return

    # Process RSS memory logs
    print("\n" + "="*60)
    print("Searching for RSS memory log files...")
    rss_log_files = find_rss_memory_logs(base_dir)
    print(f"Found {len(rss_log_files)} result directories with RSS memory logs.")

    rss_results = {}

    for dir_name, log_file in sorted(rss_log_files.items()):
        print(f"\nProcessing RSS: {dir_name}")
        print(f"  Log file: {os.path.basename(log_file)}")

        rss_data = parse_rss_memory_log(log_file)
        print(f"  Sampled {len(rss_data)} data points (every minute)")

        if rss_data:
            rss_values = [rss_mb for _, _, rss_mb in rss_data]
            min_rss = min(rss_values)
            max_rss = max(rss_values)
            avg_rss = sum(rss_values) / len(rss_values)
            print(f"  Min RSS: {min_rss:.2f} MB, Max RSS: {max_rss:.2f} MB, Avg RSS: {avg_rss:.2f} MB")

            rss_results[dir_name] = rss_data

    output_file = os.path.join(base_dir, 'qps_report.html')
    print(f"\nGenerating HTML report: {output_file}")
    generate_html_report(qps_results, rss_results, output_file)
    print(f"Report generated successfully!")
    print(f"\nOpen the report in your browser:")
    print(f"  file://{output_file}")


if __name__ == '__main__':
    main()
