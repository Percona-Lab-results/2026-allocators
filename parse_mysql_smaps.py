#!/usr/bin/env python3
"""
Parse MySQL /proc/<pid>/smaps log files and aggregate memory statistics by mapping type.

Usage: python3 parse_mysql_smaps.py <datetime>
Example: python3 parse_mysql_smaps.py 20260529_052029
"""

import sys
import re
import csv
from pathlib import Path
from typing import Dict, List, Optional


class MappingEntry:
    """Represents a single memory mapping with its smaps data."""

    def __init__(self, address_line: str):
        self.address_line = address_line
        self.size_kb = 0
        self.rss_kb = 0
        self.anonymous_kb = 0
        self.is_anonymous = False
        self.is_heap = False
        self.is_allocator_managed = False
        self.pathname = ""
        self.vmflags = ""

        # Parse the address line
        # Format: address-address perms offset dev inode [pathname]
        parts = address_line.split(None, 5)
        if len(parts) >= 4:
            self.dev = parts[3]
            # Anonymous mappings have dev "00:00" and inode 0
            if self.dev == "00:00":
                self.is_anonymous = True

            # Check for [heap] marker
            if len(parts) == 6:
                self.pathname = parts[5].strip()
                if self.pathname == "[heap]":
                    self.is_heap = True

    def add_field(self, key: str, value_str: str):
        """Add a smaps field to this mapping."""
        # Handle VmFlags specially
        if key == "VmFlags":
            self.vmflags = value_str.strip()
            # Check for allocator management flags: hg, nh, or mg
            # hg = Huge Page Advisor, nh = No Huge Page, mg = Mergeable
            if any(flag in self.vmflags.split() for flag in ['hg', 'nh', 'mg']):
                self.is_allocator_managed = True
            return

        # Extract numeric value (remove 'kB' suffix)
        value_match = re.match(r'(\d+)', value_str)
        if not value_match:
            return

        value = int(value_match.group(1))

        if key == "Size":
            self.size_kb = value
        elif key == "Rss":
            self.rss_kb = value
        elif key == "Anonymous":
            self.anonymous_kb = value


class SnapshotStats:
    """Aggregated statistics for a single snapshot."""

    def __init__(self, timestamp: str):
        self.timestamp = timestamp

        # Counters
        self.total_mapping_count = 0
        self.anon_mapping_count = 0
        self.heap_mapping_count = 0
        self.allocator_managed_count = 0
        self.file_mapping_count = 0

        # Anonymous mappings (no file backing, dev 00:00)
        self.anon_vsz_kb = 0
        self.anon_rss_kb = 0

        # Heap-specific (traditional [heap] marker)
        self.heap_size_kb = 0
        self.heap_rss_kb = 0

        # Allocator-managed (detected via VmFlags: hg, nh, mg)
        self.allocator_managed_size_kb = 0
        self.allocator_managed_rss_kb = 0

    def add_mapping(self, mapping: MappingEntry):
        """Add a mapping to the statistics."""
        self.total_mapping_count += 1

        if mapping.is_heap:
            self.heap_mapping_count += 1
            self.heap_size_kb += mapping.size_kb
            self.heap_rss_kb += mapping.rss_kb

        if mapping.is_allocator_managed:
            self.allocator_managed_count += 1
            self.allocator_managed_size_kb += mapping.size_kb
            self.allocator_managed_rss_kb += mapping.rss_kb

        if mapping.is_anonymous:
            self.anon_mapping_count += 1
            self.anon_vsz_kb += mapping.size_kb
            self.anon_rss_kb += mapping.rss_kb
        else:
            # File-backed mapping
            self.file_mapping_count += 1

    def to_dict(self) -> Dict[str, any]:
        """Convert to dictionary for CSV output."""
        # Convert kB to GB (1 GB = 1024 * 1024 kB)
        kb_to_gb = 1024 * 1024

        anon_unfaulted_kb = self.anon_vsz_kb - self.anon_rss_kb

        return {
            'Timestamp': self.timestamp,
            'total_mapping_count': self.total_mapping_count,
            'anon_mapping_count': self.anon_mapping_count,
            'heap_mapping_count': self.heap_mapping_count,
            'allocator_managed_count': self.allocator_managed_count,
            'file_mapping_count': self.file_mapping_count,
            'anon_vsz_gb': round(self.anon_vsz_kb / kb_to_gb, 3),
            'anon_rss_gb': round(self.anon_rss_kb / kb_to_gb, 3),
            'anon_unfaulted_gb': round(anon_unfaulted_kb / kb_to_gb, 3),
            'heap_size_gb': round(self.heap_size_kb / kb_to_gb, 3),
            'heap_rss_gb': round(self.heap_rss_kb / kb_to_gb, 3),
            'allocator_managed_size_gb': round(self.allocator_managed_size_kb / kb_to_gb, 3),
            'allocator_managed_rss_gb': round(self.allocator_managed_rss_kb / kb_to_gb, 3),
        }


def parse_smaps_file(file_path: str) -> List[SnapshotStats]:
    """
    Parse the MySQL smaps log file and extract aggregated statistics per snapshot.

    Returns a list of SnapshotStats objects, one per timestamp.
    """
    snapshots = []
    current_timestamp = None
    current_stats = None
    current_mapping = None

    with open(file_path, 'r') as f:
        for line in f:
            line = line.rstrip('\n')

            # Skip comments and empty lines at the start
            if not line or line.startswith('#'):
                continue

            # Check for timestamp marker
            timestamp_match = re.match(r'^===\s+(.+?)\s+===$', line)
            if timestamp_match:
                # Save previous snapshot if it exists
                if current_stats:
                    snapshots.append(current_stats)

                # Start new snapshot
                current_timestamp = timestamp_match.group(1)
                current_stats = SnapshotStats(current_timestamp)
                current_mapping = None
                continue

            # Check if this is a new mapping line (starts with hex address)
            # Format: address-address perms offset dev inode [pathname]
            mapping_match = re.match(r'^([0-9a-f]+)-([0-9a-f]+)\s+', line)
            if mapping_match:
                # Save previous mapping if it exists
                if current_mapping and current_stats:
                    current_stats.add_mapping(current_mapping)

                # Start new mapping
                current_mapping = MappingEntry(line)
                continue

            # Parse smaps fields (key: value kB format)
            if ':' in line and current_mapping:
                parts = line.split(':', 1)
                key = parts[0].strip()
                value = parts[1].strip()
                current_mapping.add_field(key, value)

    # Don't forget the last mapping and snapshot
    if current_mapping and current_stats:
        current_stats.add_mapping(current_mapping)

    if current_stats:
        snapshots.append(current_stats)

    return snapshots


def write_to_csv(snapshots: List[SnapshotStats], output_file: str):
    """Write the aggregated snapshot statistics to a CSV file."""
    if not snapshots:
        print("No snapshots found to write")
        return

    # Define field order
    fieldnames = [
        'Timestamp',
        'total_mapping_count',
        'anon_mapping_count',
        'heap_mapping_count',
        'allocator_managed_count',
        'file_mapping_count',
        'anon_vsz_gb',
        'anon_rss_gb',
        'anon_unfaulted_gb',
        'heap_size_gb',
        'heap_rss_gb',
        'allocator_managed_size_gb',
        'allocator_managed_rss_gb',
    ]

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for snapshot in snapshots:
            writer.writerow(snapshot.to_dict())

    print(f"CSV file created: {output_file}")
    print(f"Total snapshots: {len(snapshots)}")


def write_summary_report(snapshots: List[SnapshotStats], output_file: str):
    """Write a summary report of the smaps analysis."""
    if not snapshots:
        print("No snapshots to analyze")
        return

    with open(output_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("MEMORY MAPPING ANALYSIS - SMAPS DATA\n")
        f.write("=" * 80 + "\n\n")

        f.write(f"Total Snapshots: {len(snapshots)}\n")
        f.write(f"Time Range: {snapshots[0].timestamp} to {snapshots[-1].timestamp}\n\n")

        # Calculate statistics across all snapshots
        first = snapshots[0].to_dict()
        last = snapshots[-1].to_dict()

        f.write("-" * 80 + "\n")
        f.write("FIRST SNAPSHOT\n")
        f.write("-" * 80 + "\n")
        f.write(f"Timestamp: {first['Timestamp']}\n")
        f.write(f"Total mappings: {first['total_mapping_count']}\n")
        f.write(f"  - Anonymous: {first['anon_mapping_count']}\n")
        f.write(f"  - Heap ([heap] marker): {first['heap_mapping_count']}\n")
        f.write(f"  - Allocator-managed (VmFlags hg/nh/mg): {first['allocator_managed_count']}\n")
        f.write(f"  - File-backed: {first['file_mapping_count']}\n")
        f.write(f"Anonymous VSZ: {first['anon_vsz_gb']:.3f} GB\n")
        f.write(f"Anonymous RSS: {first['anon_rss_gb']:.3f} GB\n")
        f.write(f"Anonymous Unfaulted: {first['anon_unfaulted_gb']:.3f} GB\n")
        f.write(f"Heap Size ([heap]): {first['heap_size_gb']:.3f} GB\n")
        f.write(f"Heap RSS ([heap]): {first['heap_rss_gb']:.3f} GB\n")
        f.write(f"Allocator-managed Size: {first['allocator_managed_size_gb']:.3f} GB\n")
        f.write(f"Allocator-managed RSS: {first['allocator_managed_rss_gb']:.3f} GB\n\n")

        f.write("-" * 80 + "\n")
        f.write("LAST SNAPSHOT\n")
        f.write("-" * 80 + "\n")
        f.write(f"Timestamp: {last['Timestamp']}\n")
        f.write(f"Total mappings: {last['total_mapping_count']}\n")
        f.write(f"  - Anonymous: {last['anon_mapping_count']}\n")
        f.write(f"  - Heap ([heap] marker): {last['heap_mapping_count']}\n")
        f.write(f"  - Allocator-managed (VmFlags hg/nh/mg): {last['allocator_managed_count']}\n")
        f.write(f"  - File-backed: {last['file_mapping_count']}\n")
        f.write(f"Anonymous VSZ: {last['anon_vsz_gb']:.3f} GB\n")
        f.write(f"Anonymous RSS: {last['anon_rss_gb']:.3f} GB\n")
        f.write(f"Anonymous Unfaulted: {last['anon_unfaulted_gb']:.3f} GB\n")
        f.write(f"Heap Size ([heap]): {last['heap_size_gb']:.3f} GB\n")
        f.write(f"Heap RSS ([heap]): {last['heap_rss_gb']:.3f} GB\n")
        f.write(f"Allocator-managed Size: {last['allocator_managed_size_gb']:.3f} GB\n")
        f.write(f"Allocator-managed RSS: {last['allocator_managed_rss_gb']:.3f} GB\n\n")

        f.write("-" * 80 + "\n")
        f.write("DELTA (Last - First)\n")
        f.write("-" * 80 + "\n")
        f.write(f"Total mappings: {last['total_mapping_count'] - first['total_mapping_count']:+d}\n")
        f.write(f"  - Anonymous: {last['anon_mapping_count'] - first['anon_mapping_count']:+d}\n")
        f.write(f"  - Heap ([heap]): {last['heap_mapping_count'] - first['heap_mapping_count']:+d}\n")
        f.write(f"  - Allocator-managed: {last['allocator_managed_count'] - first['allocator_managed_count']:+d}\n")
        f.write(f"  - File-backed: {last['file_mapping_count'] - first['file_mapping_count']:+d}\n")
        f.write(f"Anonymous VSZ: {last['anon_vsz_gb'] - first['anon_vsz_gb']:+.3f} GB\n")
        f.write(f"Anonymous RSS: {last['anon_rss_gb'] - first['anon_rss_gb']:+.3f} GB\n")
        f.write(f"Anonymous Unfaulted: {last['anon_unfaulted_gb'] - first['anon_unfaulted_gb']:+.3f} GB\n")
        f.write(f"Heap Size ([heap]): {last['heap_size_gb'] - first['heap_size_gb']:+.3f} GB\n")
        f.write(f"Heap RSS ([heap]): {last['heap_rss_gb'] - first['heap_rss_gb']:+.3f} GB\n")
        f.write(f"Allocator-managed Size: {last['allocator_managed_size_gb'] - first['allocator_managed_size_gb']:+.3f} GB\n")
        f.write(f"Allocator-managed RSS: {last['allocator_managed_rss_gb'] - first['allocator_managed_rss_gb']:+.3f} GB\n\n")

        # Peak values
        f.write("-" * 80 + "\n")
        f.write("PEAK VALUES\n")
        f.write("-" * 80 + "\n")

        all_data = [s.to_dict() for s in snapshots]

        max_anon_vsz = max(all_data, key=lambda x: x['anon_vsz_gb'])
        f.write(f"Peak Anonymous VSZ: {max_anon_vsz['anon_vsz_gb']:.3f} GB at {max_anon_vsz['Timestamp']}\n")

        max_anon_rss = max(all_data, key=lambda x: x['anon_rss_gb'])
        f.write(f"Peak Anonymous RSS: {max_anon_rss['anon_rss_gb']:.3f} GB at {max_anon_rss['Timestamp']}\n")

        max_anon_unfaulted = max(all_data, key=lambda x: x['anon_unfaulted_gb'])
        f.write(f"Peak Anonymous Unfaulted: {max_anon_unfaulted['anon_unfaulted_gb']:.3f} GB at {max_anon_unfaulted['Timestamp']}\n")

        max_heap_size = max(all_data, key=lambda x: x['heap_size_gb'])
        f.write(f"Peak Heap Size ([heap]): {max_heap_size['heap_size_gb']:.3f} GB at {max_heap_size['Timestamp']}\n")

        max_heap_rss = max(all_data, key=lambda x: x['heap_rss_gb'])
        f.write(f"Peak Heap RSS ([heap]): {max_heap_rss['heap_rss_gb']:.3f} GB at {max_heap_rss['Timestamp']}\n")

        max_allocator_size = max(all_data, key=lambda x: x['allocator_managed_size_gb'])
        f.write(f"Peak Allocator-managed Size: {max_allocator_size['allocator_managed_size_gb']:.3f} GB at {max_allocator_size['Timestamp']}\n")

        max_allocator_rss = max(all_data, key=lambda x: x['allocator_managed_rss_gb'])
        f.write(f"Peak Allocator-managed RSS: {max_allocator_rss['allocator_managed_rss_gb']:.3f} GB at {max_allocator_rss['Timestamp']}\n")

        max_mappings = max(all_data, key=lambda x: x['total_mapping_count'])
        f.write(f"Peak Total Mappings: {max_mappings['total_mapping_count']} at {max_mappings['Timestamp']}\n")

        max_anon_mappings = max(all_data, key=lambda x: x['anon_mapping_count'])
        f.write(f"Peak Anonymous Mappings: {max_anon_mappings['anon_mapping_count']} at {max_anon_mappings['Timestamp']}\n")

        max_allocator_mappings = max(all_data, key=lambda x: x['allocator_managed_count'])
        f.write(f"Peak Allocator-managed Mappings: {max_allocator_mappings['allocator_managed_count']} at {max_allocator_mappings['Timestamp']}\n")

    print(f"Summary report created: {output_file}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 parse_mysql_smaps.py <datetime>")
        print("Example: python3 parse_mysql_smaps.py 20260529_052029")
        sys.exit(1)

    datetime_str = sys.argv[1]

    # Get script directory
    script_dir = Path(__file__).parent

    # Find matching smaps file
    pattern = f"*_mysql_smaps_{datetime_str}.log"
    matches = list(script_dir.glob(pattern))

    if not matches:
        # Try in results subdirectory
        pattern = f"results/*_mysql_smaps_{datetime_str}.log"
        matches = list(script_dir.glob(pattern))

    if not matches:
        print(f"Error: No file found matching pattern *_mysql_smaps_{datetime_str}.log")
        print(f"Searched in: {script_dir} and {script_dir}/results")
        sys.exit(1)

    if len(matches) > 1:
        print(f"Warning: Multiple files found matching pattern:")
        for match in matches:
            print(f"  - {match}")
        print(f"Using: {matches[0]}")

    smaps_file = matches[0]
    print(f"Processing smaps file: {smaps_file}")
    print("This may take a while for large files...\n")

    # Parse the smaps file
    snapshots = parse_smaps_file(str(smaps_file))

    if not snapshots:
        print("Error: No snapshot data found in the smaps file")
        sys.exit(1)

    # Generate output filenames
    base_name = smaps_file.stem
    output_dir = smaps_file.parent

    # Write statistics CSV
    stats_csv = output_dir / f"{base_name}_memory_stats.csv"
    write_to_csv(snapshots, str(stats_csv))

    # Write summary report
    summary_report = output_dir / f"{base_name}_summary.txt"
    write_summary_report(snapshots, str(summary_report))

    print("\nQuick Summary:")
    first = snapshots[0].to_dict()
    last = snapshots[-1].to_dict()
    print(f"  Snapshots analyzed: {len(snapshots)}")
    print(f"  Anonymous RSS: {first['anon_rss_gb']:.3f} GB → {last['anon_rss_gb']:.3f} GB (Δ {last['anon_rss_gb'] - first['anon_rss_gb']:+.3f} GB)")
    print(f"  Anonymous VSZ: {first['anon_vsz_gb']:.3f} GB → {last['anon_vsz_gb']:.3f} GB (Δ {last['anon_vsz_gb'] - first['anon_vsz_gb']:+.3f} GB)")
    print(f"  Total mappings: {first['total_mapping_count']} → {last['total_mapping_count']} (Δ {last['total_mapping_count'] - first['total_mapping_count']:+d})")


if __name__ == "__main__":
    main()
