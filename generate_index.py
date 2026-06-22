#!/usr/bin/env python3
"""
Generate index.html for all HTML reports in the current directory.
"""

import os
from pathlib import Path
from datetime import datetime

def get_file_info(filepath):
    """Get file modification time and size."""
    stat = filepath.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
    size_mb = stat.st_size / (1024 * 1024)
    return mtime, size_mb

def get_description(filename):
    """Return description based on filename pattern."""
    name = filename.lower()

    if 'arenas' in name:
        return "Report showing the effect of changing MALLOC_ARENA_MAX from 8 to 2 in glibc"
    elif 'cgroups' in name:
        return "Report showing the effect of limiting memory available to mysqld using cgroups"
    elif 'breaks' in name:
        return "Report with multiple runs and breaks to help reclaim memory"
    elif name == 'qps_report-8.4.9.html':
        return "General report about all memory allocators performance in Percona Server 8.4.9"
    elif 'myrocks' in name:
        return "Performance report for MyRocks storage engine with different memory allocators"
    elif '9.7.0' in name:
        return "Performance report for Percona Server 9.7.0 with different memory allocators"
    else:
        return "Performance report with different memory allocators"

def generate_index():
    """Generate index.html with all HTML reports."""
    current_dir = Path.cwd()
    html_files = sorted([f for f in current_dir.glob('*.html') if f.name != 'index.html'])

    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Memory Allocators Performance Reports - 2026</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
            line-height: 1.6;
        }
        h1 {
            color: #333;
            border-bottom: 3px solid #0066cc;
            padding-bottom: 10px;
        }
        .report-list {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            padding: 20px;
            margin-top: 20px;
        }
        .report-item {
            padding: 15px;
            margin: 10px 0;
            border-left: 4px solid #0066cc;
            background-color: #f9f9f9;
            transition: background-color 0.2s;
        }
        .report-item:hover {
            background-color: #e8f4ff;
        }
        .report-link {
            font-size: 1.2em;
            font-weight: bold;
            color: #0066cc;
            text-decoration: none;
        }
        .report-link:hover {
            text-decoration: underline;
        }
        .report-description {
            color: #666;
            margin: 8px 0;
        }
        .report-meta {
            font-size: 0.9em;
            color: #999;
        }
        .footer {
            margin-top: 40px;
            text-align: center;
            color: #999;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <h1>Memory Allocators Performance Reports - 2026</h1>
    <div class="report-list">
"""

    if not html_files:
        html_content += "        <p>No HTML reports found in this directory.</p>\n"
    else:
        for html_file in html_files:
            filename = html_file.name
            mtime, size_mb = get_file_info(html_file)
            description = get_description(filename)

            html_content += f"""        <div class="report-item">
            <a href="{filename}" class="report-link">{filename}</a>
            <div class="report-description">{description}</div>
            <div class="report-meta">Modified: {mtime} | Size: {size_mb:.2f} MB</div>
        </div>
"""

    html_content += """    </div>
    <div class="footer">
        <p>Generated on """ + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """</p>
    </div>
</body>
</html>
"""

    output_file = current_dir / 'index.html'
    output_file.write_text(html_content, encoding='utf-8')
    print(f"✓ Generated {output_file}")
    print(f"  Found {len(html_files)} report(s)")

if __name__ == '__main__':
    generate_index()
