#!/usr/bin/env python3
"""
Generate HTML report with RSS/VSZ memory graphs from benchmark results.
Parses nothp_jemalloc53_rss_memory_*.log files (CSV format:
"Timestamp, VmRSS_KB, VmSize_KB") and plots RSS and VSZ over time.
"""

import os
import re
import glob
import sys
import argparse
from datetime import datetime
from pathlib import Path
import json


def find_rss_memory_logs(data_dir):
    """
    Find all nothp_jemalloc53_rss_memory_*.log files in subdirectories.
    Returns a dict: {result_dir_name: log_file_path}
    """
    results = {}

    if not os.path.exists(data_dir):
        return results

    # Find all results-* directories
    for entry in os.listdir(data_dir):
        result_dir = os.path.join(data_dir, entry)

        # Skip non-directories and .tar.gz files
        if not os.path.isdir(result_dir):
            continue

        # Find rss memory log files
        pattern = os.path.join(result_dir, 'nothp_jemalloc53_rss_memory_*.log')
        log_files = glob.glob(pattern)

        if log_files:
            # Use the first log file found
            results[entry] = log_files[0]

    return results


def parse_rss_memory_log(log_file):
    """
    Parse a CSV-style rss_memory log file:
        # mysqld memory log (every 30 seconds), PID 1973612
        # Timestamp, VmRSS_KB, VmSize_KB
        2026-07-27 04:29:06, 5163848, 6336988
    Returns a list of (timestamp, elapsed_seconds, rss_mb, vsz_mb) tuples.
    """
    data_points = []
    start_time = None

    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue

            # Match "2026-07-27 04:29:06, 5163848, 6336988"
            match = re.match(r'^([\d-]+\s+[\d:]+)\s*,\s*(\d+)\s*,\s*(\d+)$', line)
            if not match:
                continue

            timestamp_str = match.group(1)
            rss_mb = int(match.group(2)) / 1024
            vsz_mb = int(match.group(3)) / 1024

            try:
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue

            if start_time is None:
                start_time = timestamp
            elapsed = (timestamp - start_time).total_seconds()
            data_points.append((timestamp_str, elapsed, rss_mb, vsz_mb))

    return data_points


def parse_result_dir_name(dir_name):
    """
    Parse result directory name to extract configuration.
    The config always contains a THP token ('thp' or 'nothp') immediately
    followed by the allocator, e.g.:
        results-ps-9.7.0-1-rel-nothp-jemalloc53-4G-nobinlog-innodb
        results-ps-8.4.9-9-CUSTOM113-nothp-glibc-150G
    Anchoring on the THP token keeps parsing correct regardless of any
    trailing suffix (e.g. -nobinlog-innodb-pool) after the buffer pool.
    """
    parts = dir_name.split('-')

    # Locate the THP token; the allocator is the part right after it.
    thp_idx = None
    for i, part in enumerate(parts):
        if part in ('thp', 'nothp'):
            thp_idx = i
            break

    if thp_idx is not None and thp_idx + 1 < len(parts):
        thp = parts[thp_idx]
        allocator = parts[thp_idx + 1]

        # Buffer pool: first "<number>G" token at or after the allocator.
        buffer_pool = 'unknown'
        for part in parts[thp_idx + 2:]:
            if re.fullmatch(r'\d+G', part):
                buffer_pool = part
                break

        # Suffix: everything before the THP token (minus the leading "results").
        suffix = '-'.join(parts[1:thp_idx])

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


def generate_html_report(rss_results, output_file):
    """
    Generate an HTML report with an interactive RSS/VSZ graph using Chart.js.
    """

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
    stats_rows = []
    for dir_name, rss_data in sorted(rss_results.items()):
        if not rss_data:
            continue

        config = parse_result_dir_name(dir_name)
        color = colors[color_idx % len(colors)]
        color_idx += 1

        rss_dataset = {
            'label': f"{config['label']} ({config['suffix']}) RSS",
            'data': [{'x': elapsed, 'y': round(rss_mb, 2)} for _, elapsed, rss_mb, _ in rss_data],
            'borderColor': color,
            'backgroundColor': color.replace('rgb', 'rgba').replace(')', ', 0.1)'),
            'tension': 0.3,
            'pointRadius': 0,
            'borderWidth': 2,
        }
        rss_datasets.append(rss_dataset)

        vsz_dataset = {
            'label': f"{config['label']} ({config['suffix']}) VSZ",
            'data': [{'x': elapsed, 'y': round(vsz_mb, 2)} for _, elapsed, _, vsz_mb in rss_data],
            'borderColor': color,
            'backgroundColor': 'transparent',
            'borderDash': [6, 4],
            'tension': 0.3,
            'pointRadius': 0,
            'borderWidth': 2,
        }
        rss_datasets.append(vsz_dataset)

        rss_values = [rss_mb for _, _, rss_mb, _ in rss_data]
        vsz_values = [vsz_mb for _, _, _, vsz_mb in rss_data]
        stats_rows.append({
            'label': f"{config['label']} ({config['suffix']})",
            'thp': config['thp'],
            'allocator': config['allocator'],
            'minRss': round(min(rss_values), 2),
            'maxRss': round(max(rss_values), 2),
            'avgRss': round(sum(rss_values) / len(rss_values), 2),
            'maxVsz': round(max(vsz_values), 2),
            'growthRss': round(rss_values[-1] - rss_values[0], 2),
            'samples': len(rss_data),
        })

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MySQL RSS/VSZ Memory Analysis</title>
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
        .reset-zoom-button {{
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
        .reset-zoom-button:active {{
            background-color: #c2150a;
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
        <h1>MySQL RSS / VSZ Memory Analysis</h1>

        <div class="info-box">
            <strong>Data Source:</strong> mysqld VmRSS / VmSize samples (every 30 seconds)
            from nothp_jemalloc53_rss_memory_*.log files
        </div>

        <div class="controls">
            <h3>Graph Controls</h3>
            <div class="checkbox-grid" id="rssCheckboxGrid">
                <!-- Checkboxes will be generated here -->
            </div>
            <button id="rssResetZoomButton" class="reset-zoom-button">Reset Zoom</button>
            <div class="zoom-info">
                💡 <strong>Tip:</strong> Click and drag on the graph to zoom into a specific area. Use the "Reset Zoom" button to return to full view.
            </div>
        </div>

        <h2>MySQL RSS / VSZ Memory Usage Over Time</h2>
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
                    <th>Min RSS (MB)</th>
                    <th>Max RSS (MB)</th>
                    <th>Avg RSS (MB)</th>
                    <th>RSS Growth (MB)</th>
                    <th>Max VSZ (MB)</th>
                    <th>Samples</th>
                </tr>
            </thead>
            <tbody id="statsTableBody">
            </tbody>
        </table>

        <div class="footer">
            Generated on {Path(output_file).name} | Memory Analysis Tool
        </div>
    </div>

    <script>
        // Chart data
        const rssDatasets = {json.dumps(rss_datasets, indent=8)};
        const statsData = {json.dumps(stats_rows, indent=8)};

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
                        text: 'MySQL RSS / VSZ Memory Usage (sampled every 30 seconds)',
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
                                return context.dataset.label + ': ' + context.parsed.y.toFixed(2) + ' MB';
                            }}
                        }}
                    }},
                    zoom: {{
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
                            text: 'Memory (MB)'
                        }},
                        beginAtZero: true,
                        min: 0
                    }}
                }}
            }}
        }});

        // Populate statistics table
        const tbody = document.getElementById('statsTableBody');
        statsData.forEach(stat => {{
            const row = tbody.insertRow();
            row.innerHTML = `
                <td>${{stat.label}}</td>
                <td>${{stat.thp}}</td>
                <td>${{stat.allocator}}</td>
                <td>${{stat.minRss}}</td>
                <td>${{stat.maxRss}}</td>
                <td>${{stat.avgRss}}</td>
                <td>${{stat.growthRss}}</td>
                <td>${{stat.maxVsz}}</td>
                <td>${{stat.samples}}</td>
            `;
        }});

        // Generate checkboxes for RSS graph visibility control
        const rssCheckboxGrid = document.getElementById('rssCheckboxGrid');

        rssDatasets.forEach((dataset, idx) => {{
            const div = document.createElement('div');
            div.className = 'checkbox-item';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.id = `rss-graph-${{idx}}`;
            checkbox.checked = true;
            checkbox.addEventListener('change', function() {{
                // Toggle visibility of RSS dataset
                rssChart.data.datasets[idx].hidden = !this.checked;
                rssChart.update();
            }});

            const label = document.createElement('label');
            label.htmlFor = `rss-graph-${{idx}}`;

            // Get the color from the dataset
            const color = dataset.borderColor;

            // Create color indicator
            const colorIndicator = document.createElement('span');
            colorIndicator.className = 'color-indicator';
            colorIndicator.style.backgroundColor = color;

            // Create text span
            const textSpan = document.createElement('span');
            textSpan.textContent = dataset.label;

            label.appendChild(colorIndicator);
            label.appendChild(textSpan);

            div.appendChild(checkbox);
            div.appendChild(label);
            rssCheckboxGrid.appendChild(div);
        }});

        // Reset zoom button for RSS chart
        const rssResetZoomButton = document.getElementById('rssResetZoomButton');
        rssResetZoomButton.addEventListener('click', function() {{
            rssChart.resetZoom();
            rssChart.options.scales.y.min = 0;
            rssChart.options.scales.y.beginAtZero = true;
            rssChart.update();
        }});
    </script>
</body>
</html>'''

    with open(output_file, 'w') as f:
        f.write(html_content)


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Generate HTML report with RSS/VSZ memory graphs from nothp_jemalloc53_rss_memory_*.log files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s mem-sp rss_report.html
  %(prog)s /path/to/results my_report.html
  %(prog)s                                    # Uses default: mem-sp/ and rss_report.html
        '''
    )
    parser.add_argument(
        'data_dir',
        nargs='?',
        default='mem-sp',
        help='Directory containing result subdirectories (default: mem-sp)'
    )
    parser.add_argument(
        'output_file',
        nargs='?',
        default='rss_report.html',
        help='Output HTML file name (default: rss_report.html)'
    )

    args = parser.parse_args()

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # If data_dir is relative, resolve it relative to script directory
    if not os.path.isabs(args.data_dir):
        data_dir = os.path.join(script_dir, args.data_dir)
    else:
        data_dir = args.data_dir

    # If output_file is relative, resolve it relative to script directory
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

    print("Searching for RSS memory log files...")
    rss_log_files = find_rss_memory_logs(data_dir)

    if not rss_log_files:
        print("No nothp_jemalloc53_rss_memory_*.log files found in subdirectories.")
        return

    print(f"Found {len(rss_log_files)} result directories with RSS memory logs.")

    rss_results = {}

    for dir_name, log_file in sorted(rss_log_files.items()):
        print(f"\nProcessing RSS: {dir_name}")
        print(f"  Log file: {os.path.basename(log_file)}")

        rss_data = parse_rss_memory_log(log_file)
        print(f"  Parsed {len(rss_data)} data points")

        if rss_data:
            rss_values = [rss_mb for _, _, rss_mb, _ in rss_data]
            vsz_values = [vsz_mb for _, _, _, vsz_mb in rss_data]
            min_rss = min(rss_values)
            max_rss = max(rss_values)
            avg_rss = sum(rss_values) / len(rss_values)
            avg_vsz = sum(vsz_values) / len(vsz_values)
            print(f"  Min RSS: {min_rss:.2f} MB, Max RSS: {max_rss:.2f} MB, Avg RSS: {avg_rss:.2f} MB, Avg VSZ: {avg_vsz:.2f} MB")

            rss_results[dir_name] = rss_data

    if not rss_results:
        print("\nNo RSS data to generate report.")
        return

    print(f"\nGenerating HTML report: {output_file}")
    generate_html_report(rss_results, output_file)
    print(f"Report generated successfully!")
    print(f"\nOpen the report in your browser:")
    print(f"  file://{output_file}")


if __name__ == '__main__':
    main()
