#!/usr/bin/env python3
"""
Parse MySQL /proc/<pid>/status and /proc/<pid>/stat log files and extract metrics to CSV.

Usage: python3 parse_mysql_status.py <datetime>
Example: python3 parse_mysql_status.py 20260528_134041
"""

import sys
import glob
import re
import csv
from pathlib import Path
from typing import Dict, List, Optional


def parse_status_file(file_path: str) -> List[Dict[str, str]]:
    """
    Parse the MySQL status log file and extract memory metrics.

    Returns a list of dictionaries, each containing timestamp and memory metrics.
    """
    records = []
    current_record = {}
    current_timestamp = None

    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Check for timestamp marker
            timestamp_match = re.match(r'^===\s+(.+?)\s+===$', line)
            if timestamp_match:
                # Save previous record if it exists
                if current_timestamp and current_record:
                    current_record['Timestamp'] = current_timestamp
                    records.append(current_record)

                # Start new record
                current_timestamp = timestamp_match.group(1)
                current_record = {}
                continue

            # Parse memory fields
            if ':' in line:
                parts = line.split(':', 1)
                key = parts[0].strip()
                value = parts[1].strip()

                # Extract only the fields we're interested in
                if key in ['VmRSS', 'VmSize', 'VmSwap', 'RssAnon', 'RssFile', 'RssShmem', 'AnonHugePages']:
                    # Extract numeric value (remove 'kB' suffix if present)
                    value_match = re.match(r'(\d+)', value)
                    if value_match:
                        current_record[key] = value_match.group(1)
                    else:
                        current_record[key] = '0'

        # Don't forget the last record
        if current_timestamp and current_record:
            current_record['Timestamp'] = current_timestamp
            records.append(current_record)

    return records


def parse_stat_file(file_path: str) -> List[Dict[str, str]]:
    """
    Parse the MySQL stat log file and extract page fault counters.

    /proc/<pid>/stat format (space-separated):
    Field 10: minflt - minor page faults
    Field 12: majflt - major page faults
    (Fields are 1-indexed in documentation, but 0-indexed in actual parsing)

    Returns a list of dictionaries, each containing timestamp and page fault counters.
    """
    records = []
    current_timestamp = None

    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Check for timestamp marker
            timestamp_match = re.match(r'^===\s+(.+?)\s+===$', line)
            if timestamp_match:
                current_timestamp = timestamp_match.group(1)
                continue

            # Parse stat line (not a timestamp marker and not empty)
            if current_timestamp and line and not line.startswith('==='):
                # Split the stat line by spaces
                # Handle process name which may contain spaces (enclosed in parentheses)
                parts = line.split()

                if len(parts) >= 12:
                    try:
                        # Field indices (0-based): minflt=9, majflt=11
                        minflt = parts[9]
                        majflt = parts[11]

                        record = {
                            'Timestamp': current_timestamp,
                            'minflt': minflt,
                            'majflt': majflt
                        }
                        records.append(record)
                    except (IndexError, ValueError) as e:
                        print(f"Warning: Failed to parse stat line: {e}")
                        continue

    return records


def write_to_csv(records: List[Dict[str, str]], output_file: str):
    """Write the parsed records to a CSV file."""
    if not records:
        print(f"No records found to write")
        return

    # Define the field order
    fieldnames = ['Timestamp', 'VmRSS', 'VmSize', 'VmSwap', 'RssAnon', 'RssFile', 'RssShmem', 'AnonHugePages']

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')

        writer.writeheader()

        for record in records:
            # Ensure all fields are present, fill with '0' if missing
            row = {field: record.get(field, '0') for field in fieldnames}
            writer.writerow(row)

    print(f"CSV file created: {output_file}")
    print(f"Total records: {len(records)}")


def write_stat_to_csv(records: List[Dict[str, str]], output_file: str):
    """Write the parsed stat records to a CSV file."""
    if not records:
        print(f"No stat records found to write")
        return

    # Define the field order
    fieldnames = ['Timestamp', 'minflt', 'majflt']

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')

        writer.writeheader()

        for record in records:
            # Ensure all fields are present, fill with '0' if missing
            row = {field: record.get(field, '0') for field in fieldnames}
            writer.writerow(row)

    print(f"CSV file created: {output_file}")
    print(f"Total records: {len(records)}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 parse_mysql_status.py <datetime>")
        print("Example: python3 parse_mysql_status.py 20260528_134041")
        sys.exit(1)

    datetime_str = sys.argv[1]

    # Get script directory
    script_dir = Path(__file__).parent

    # Find matching status file
    pattern = f"*_mysql_status_{datetime_str}.log"
    matches = list(script_dir.glob(pattern))

    if not matches:
        # Try in results subdirectory
        pattern = f"results/*_mysql_status_{datetime_str}.log"
        matches = list(script_dir.glob(pattern))

    if not matches:
        print(f"Error: No file found matching pattern *_mysql_status_{datetime_str}.log")
        print(f"Searched in: {script_dir} and {script_dir}/results")
        sys.exit(1)

    if len(matches) > 1:
        print(f"Warning: Multiple files found matching pattern:")
        for match in matches:
            print(f"  - {match}")
        print(f"Using: {matches[0]}")

    status_file = matches[0]
    print(f"Processing status file: {status_file}")

    # Parse the status file
    status_records = parse_status_file(str(status_file))

    if not status_records:
        print("Error: No data records found in the status file")
        sys.exit(1)

    # Generate output filename for status
    output_file = status_file.stem + "_memory_metrics.csv"
    output_path = status_file.parent / output_file

    # Write status to CSV
    write_to_csv(status_records, str(output_path))

    # Now process the stat file
    print(f"\nLooking for stat file...")
    stat_pattern = f"*_mysql_stat_{datetime_str}.log"
    stat_matches = list(script_dir.glob(stat_pattern))

    if not stat_matches:
        # Try in results subdirectory
        stat_pattern = f"results/*_mysql_stat_{datetime_str}.log"
        stat_matches = list(script_dir.glob(stat_pattern))

    if not stat_matches:
        print(f"Warning: No stat file found matching pattern *_mysql_stat_{datetime_str}.log")
        print(f"Skipping page fault statistics")
        return

    if len(stat_matches) > 1:
        print(f"Warning: Multiple stat files found matching pattern:")
        for match in stat_matches:
            print(f"  - {match}")
        print(f"Using: {stat_matches[0]}")

    stat_file = stat_matches[0]
    print(f"Processing stat file: {stat_file}")

    # Parse the stat file
    stat_records = parse_stat_file(str(stat_file))

    if not stat_records:
        print("Warning: No data records found in the stat file")
        return

    # Generate output filename for stat
    stat_output_file = stat_file.stem + "_pagefaults.csv"
    stat_output_path = stat_file.parent / stat_output_file

    # Write stat to CSV
    write_stat_to_csv(stat_records, str(stat_output_path))


if __name__ == "__main__":
    main()
