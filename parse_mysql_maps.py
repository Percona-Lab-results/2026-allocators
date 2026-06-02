#!/usr/bin/env python3
"""
Parse MySQL /proc/<pid>/maps log files and analyze mapping stability.

Usage: python3 parse_mysql_maps.py <datetime>
Example: python3 parse_mysql_maps.py 20260529_052029
"""

import sys
import re
import csv
from pathlib import Path
from typing import Dict, List, Set, Tuple
from collections import defaultdict


def parse_maps_file(file_path: str) -> Tuple[List[Dict], Dict[str, any]]:
    """
    Parse the MySQL maps log file and extract mapping information.

    Returns:
        - List of snapshot records (timestamp, line_count, new_mappings)
        - Analysis dictionary with stability metrics
    """
    snapshots = []
    current_timestamp = None
    current_mappings = []

    # Track mappings across all snapshots
    all_mappings_ever = set()  # All unique mappings that ever appeared
    mapping_appearances = defaultdict(list)  # mapping -> list of snapshot indices
    previous_snapshot_mappings = set()

    snapshot_idx = 0

    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue

            # Check for timestamp marker
            timestamp_match = re.match(r'^===\s+(.+?)\s+===$', line)
            if timestamp_match:
                # Save previous snapshot if it exists
                if current_timestamp and current_mappings:
                    current_set = set(current_mappings)
                    new_mappings = len(current_set - previous_snapshot_mappings)

                    snapshot_record = {
                        'Timestamp': current_timestamp,
                        'SnapshotIndex': snapshot_idx,
                        'TotalMappings': len(current_mappings),
                        'NewMappings': new_mappings,
                        'UniqueMappings': len(current_set)
                    }
                    snapshots.append(snapshot_record)

                    # Track appearances
                    for mapping in current_set:
                        mapping_appearances[mapping].append(snapshot_idx)

                    all_mappings_ever.update(current_set)
                    previous_snapshot_mappings = current_set
                    snapshot_idx += 1

                # Start new snapshot
                current_timestamp = timestamp_match.group(1)
                current_mappings = []
                continue

            # Parse mapping line: extract address range
            # Format: address-address perms offset dev inode [pathname]
            mapping_match = re.match(r'^([0-9a-f]+)-([0-9a-f]+)\s+', line)
            if mapping_match:
                addr_start = mapping_match.group(1)
                addr_end = mapping_match.group(2)
                # Use address range as the mapping identifier
                mapping_id = f"{addr_start}-{addr_end}"
                current_mappings.append(mapping_id)

    # Don't forget the last snapshot
    if current_timestamp and current_mappings:
        current_set = set(current_mappings)
        new_mappings = len(current_set - previous_snapshot_mappings)

        snapshot_record = {
            'Timestamp': current_timestamp,
            'SnapshotIndex': snapshot_idx,
            'TotalMappings': len(current_mappings),
            'NewMappings': new_mappings,
            'UniqueMappings': len(current_set)
        }
        snapshots.append(snapshot_record)

        for mapping in current_set:
            mapping_appearances[mapping].append(snapshot_idx)

        all_mappings_ever.update(current_set)

    # Calculate stability metrics
    stability_analysis = analyze_stability(mapping_appearances, len(snapshots))

    # Add overall statistics
    stability_analysis['total_snapshots'] = len(snapshots)
    stability_analysis['total_unique_mappings'] = len(all_mappings_ever)

    return snapshots, stability_analysis


def analyze_stability(mapping_appearances: Dict[str, List[int]],
                      total_snapshots: int) -> Dict[str, any]:
    """
    Analyze mapping stability based on appearance patterns.

    A mapping is "stable" if it appears in consecutive snapshots.
    """
    stable_mappings = []
    unstable_mappings = []
    single_appearance = []

    # Analyze each mapping's appearance pattern
    for mapping, appearances in mapping_appearances.items():
        appearance_count = len(appearances)

        if appearance_count == 1:
            single_appearance.append(mapping)
            continue

        # Check if appearances are consecutive
        is_stable = True
        for i in range(len(appearances) - 1):
            if appearances[i+1] - appearances[i] > 1:
                is_stable = False
                break

        if is_stable:
            stable_mappings.append({
                'mapping': mapping,
                'first_appearance': appearances[0],
                'last_appearance': appearances[-1],
                'duration': appearances[-1] - appearances[0] + 1,
                'appearances': appearance_count
            })
        else:
            unstable_mappings.append({
                'mapping': mapping,
                'appearances': appearance_count,
                'pattern': appearances
            })

    # Sort stable mappings by duration (longest first)
    stable_mappings.sort(key=lambda x: x['duration'], reverse=True)

    return {
        'stable_count': len(stable_mappings),
        'unstable_count': len(unstable_mappings),
        'single_appearance_count': len(single_appearance),
        'stable_mappings': stable_mappings,
        'unstable_mappings': unstable_mappings,
        'single_appearance': single_appearance
    }


def write_snapshots_to_csv(snapshots: List[Dict], output_file: str):
    """Write snapshot metrics to a CSV file."""
    if not snapshots:
        print("No snapshots found to write")
        return

    fieldnames = ['Timestamp', 'SnapshotIndex', 'TotalMappings',
                  'NewMappings', 'UniqueMappings']

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for record in snapshots:
            writer.writerow(record)

    print(f"Snapshot CSV file created: {output_file}")
    print(f"Total snapshots: {len(snapshots)}")


def write_stability_report(analysis: Dict, output_file: str):
    """Write detailed stability analysis to a text file."""
    with open(output_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("MEMORY MAPPING STABILITY ANALYSIS\n")
        f.write("=" * 80 + "\n\n")

        f.write(f"Total Snapshots: {analysis['total_snapshots']}\n")
        f.write(f"Total Unique Mappings (across all snapshots): {analysis['total_unique_mappings']}\n\n")

        f.write(f"Stable Mappings (consecutive appearances): {analysis['stable_count']}\n")
        f.write(f"Unstable Mappings (non-consecutive appearances): {analysis['unstable_count']}\n")
        f.write(f"Single Appearance Mappings: {analysis['single_appearance_count']}\n\n")

        # Report on stable mappings
        f.write("-" * 80 + "\n")
        f.write("TOP 20 MOST STABLE MAPPINGS (longest consecutive duration)\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'Address Range':<30} {'First':<8} {'Last':<8} {'Duration':<10} {'Appears':<10}\n")
        f.write("-" * 80 + "\n")

        for mapping_info in analysis['stable_mappings'][:20]:
            f.write(f"{mapping_info['mapping']:<30} "
                   f"{mapping_info['first_appearance']:<8} "
                   f"{mapping_info['last_appearance']:<8} "
                   f"{mapping_info['duration']:<10} "
                   f"{mapping_info['appearances']:<10}\n")

        # Report on unstable mappings (sample)
        f.write("\n" + "-" * 80 + "\n")
        f.write("SAMPLE UNSTABLE MAPPINGS (first 20)\n")
        f.write("-" * 80 + "\n")
        f.write(f"{'Address Range':<30} {'Appearances':<12} {'Pattern (first 10 indices)':<40}\n")
        f.write("-" * 80 + "\n")

        for mapping_info in analysis['unstable_mappings'][:20]:
            pattern_str = str(mapping_info['pattern'][:10])
            if len(mapping_info['pattern']) > 10:
                pattern_str += "..."
            f.write(f"{mapping_info['mapping']:<30} "
                   f"{mapping_info['appearances']:<12} "
                   f"{pattern_str:<40}\n")

        # Statistics on mapping persistence
        f.write("\n" + "-" * 80 + "\n")
        f.write("MAPPING PERSISTENCE STATISTICS\n")
        f.write("-" * 80 + "\n")

        if analysis['stable_mappings']:
            durations = [m['duration'] for m in analysis['stable_mappings']]
            avg_duration = sum(durations) / len(durations)
            max_duration = max(durations)
            min_duration = min(durations)

            f.write(f"Average stable mapping duration: {avg_duration:.2f} snapshots\n")
            f.write(f"Maximum stable mapping duration: {max_duration} snapshots\n")
            f.write(f"Minimum stable mapping duration: {min_duration} snapshots\n")

            # Count mappings that lasted entire duration
            full_duration = [m for m in analysis['stable_mappings']
                           if m['duration'] == analysis['total_snapshots']]
            f.write(f"Mappings present in all snapshots: {len(full_duration)}\n")

    print(f"Stability report created: {output_file}")


def write_stable_mappings_csv(analysis: Dict, output_file: str):
    """Write stable mappings details to CSV."""
    if not analysis['stable_mappings']:
        print("No stable mappings to write")
        return

    fieldnames = ['AddressRange', 'FirstAppearance', 'LastAppearance',
                  'Duration', 'Appearances']

    with open(output_file, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for mapping_info in analysis['stable_mappings']:
            writer.writerow({
                'AddressRange': mapping_info['mapping'],
                'FirstAppearance': mapping_info['first_appearance'],
                'LastAppearance': mapping_info['last_appearance'],
                'Duration': mapping_info['duration'],
                'Appearances': mapping_info['appearances']
            })

    print(f"Stable mappings CSV created: {output_file}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 parse_mysql_maps.py <datetime>")
        print("Example: python3 parse_mysql_maps.py 20260529_052029")
        sys.exit(1)

    datetime_str = sys.argv[1]

    # Get script directory
    script_dir = Path(__file__).parent

    # Find matching maps file
    pattern = f"*_mysql_maps_{datetime_str}.log"
    matches = list(script_dir.glob(pattern))

    if not matches:
        # Try in results subdirectory
        pattern = f"results/*_mysql_maps_{datetime_str}.log"
        matches = list(script_dir.glob(pattern))

    if not matches:
        print(f"Error: No file found matching pattern *_mysql_maps_{datetime_str}.log")
        print(f"Searched in: {script_dir} and {script_dir}/results")
        sys.exit(1)

    if len(matches) > 1:
        print(f"Warning: Multiple files found matching pattern:")
        for match in matches:
            print(f"  - {match}")
        print(f"Using: {matches[0]}")

    maps_file = matches[0]
    print(f"Processing maps file: {maps_file}")
    print("This may take a while for large files...\n")

    # Parse the maps file
    snapshots, stability_analysis = parse_maps_file(str(maps_file))

    if not snapshots:
        print("Error: No snapshot data found in the maps file")
        sys.exit(1)

    # Generate output filenames
    base_name = maps_file.stem
    output_dir = maps_file.parent

    # Write snapshots CSV
    snapshots_csv = output_dir / f"{base_name}_snapshots.csv"
    write_snapshots_to_csv(snapshots, str(snapshots_csv))

    # Write stability report
    stability_report = output_dir / f"{base_name}_stability_report.txt"
    write_stability_report(stability_analysis, str(stability_report))

    # Write stable mappings CSV
    stable_csv = output_dir / f"{base_name}_stable_mappings.csv"
    write_stable_mappings_csv(stability_analysis, str(stable_csv))

    print("\nSummary:")
    print(f"  Total snapshots analyzed: {stability_analysis['total_snapshots']}")
    print(f"  Total unique mappings: {stability_analysis['total_unique_mappings']}")
    print(f"  Stable mappings: {stability_analysis['stable_count']}")
    print(f"  Unstable mappings: {stability_analysis['unstable_count']}")
    print(f"  Single appearance: {stability_analysis['single_appearance_count']}")


if __name__ == "__main__":
    main()
