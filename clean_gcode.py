#!/usr/bin/env python3
"""
clean_gcode.py

Cleans 3D print G-code files (from Bambu Studio / OrcaSlicer) by removing:
- Everything before '; MACHINE_START_GCODE_END'
- Everything after '; MACHINE_END_GCODE_START'
- All skippable timelapse blocks (including the skippable comments):
    ; SKIPPABLE_START
    ; SKIPTYPE: timelapse
    ...
    ; SKIPPABLE_END

Accepts:
- Standalone *.gcode files
- *.gcode.3mf / *.3mf archive files (extracts and cleans each plate)
- Folders (scans and cleans all *.gcode and *.gcode.3mf files in the folder)
"""

import argparse
import os
import re
import sys
import zipfile
from typing import Dict, List, Optional, Set, Tuple


START_MARKER_REGEX = re.compile(r"^\s*;\s*MACHINE_START_GCODE_END\b")
END_MARKER_REGEX = re.compile(r"^\s*;\s*MACHINE_END_GCODE_START\b")
SKIPPABLE_START_REGEX = re.compile(r"^\s*;\s*SKIPPABLE_START\b")
SKIPPABLE_END_REGEX = re.compile(r"^\s*;\s*SKIPPABLE_END\b")
SKIPTYPE_TIMELAPSE_REGEX = re.compile(r"^\s*;\s*SKIPTYPE:\s*timelapse\b", re.IGNORECASE)


def natural_sort_key(s: str) -> List:
    """Sort strings containing numbers naturally (e.g., plate_1, plate_2, plate_10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def clean_gcode(
    lines: List[str],
    remove_boundary_markers: bool = False,
    all_skippable: bool = False,
) -> Tuple[List[str], Dict[str, int]]:
    """
    Cleans G-code lines according to the specified rules.

    Args:
        lines: List of G-code lines (with newline characters).
        remove_boundary_markers: If True, also remove the boundary markers
                                 '; MACHINE_START_GCODE_END' and '; MACHINE_END_GCODE_START'.
                                 Default is False (retains boundary markers).
        all_skippable: If True, removes all skippable blocks regardless of skip type.
                       Default is False (removes blocks with SKIPTYPE: timelapse).

    Returns:
        (cleaned_lines, stats_dict)
    """
    total_lines = len(lines)

    # 1. Locate start marker: ; MACHINE_START_GCODE_END
    start_idx = None
    for idx, line in enumerate(lines):
        if START_MARKER_REGEX.match(line):
            start_idx = idx
            break

    if start_idx is None:
        raise ValueError("Could not find start marker: '; MACHINE_START_GCODE_END'")

    # 2. Locate end marker: ; MACHINE_END_GCODE_START
    end_idx = None
    for idx in range(total_lines - 1, -1, -1):
        if END_MARKER_REGEX.match(lines[idx]):
            end_idx = idx
            break

    if end_idx is None:
        raise ValueError("Could not find end marker: '; MACHINE_END_GCODE_START'")

    if end_idx < start_idx:
        raise ValueError(
            f"End marker found before start marker (start line {start_idx + 1}, end line {end_idx + 1})"
        )

    # Trim lines before start marker and after end marker
    slice_start = start_idx + 1 if remove_boundary_markers else start_idx
    slice_end = end_idx if remove_boundary_markers else end_idx + 1
    trimmed = lines[slice_start:slice_end]

    # 3. Filter out skippable timelapse blocks
    cleaned: List[str] = []
    in_skippable = False
    skippable_buffer: List[str] = []
    timelapse_blocks_removed = 0
    timelapse_lines_removed = 0

    for line in trimmed:
        if SKIPPABLE_START_REGEX.match(line):
            in_skippable = True
            skippable_buffer = [line]
        elif in_skippable:
            skippable_buffer.append(line)
            if SKIPPABLE_END_REGEX.match(line):
                in_skippable = False
                is_timelapse = all_skippable or any(
                    SKIPTYPE_TIMELAPSE_REGEX.match(l) for l in skippable_buffer
                )
                if is_timelapse:
                    timelapse_blocks_removed += 1
                    timelapse_lines_removed += len(skippable_buffer)
                else:
                    cleaned.extend(skippable_buffer)
                skippable_buffer = []
        else:
            cleaned.append(line)

    # Append any remaining buffer if skippable was unclosed
    if skippable_buffer:
        cleaned.extend(skippable_buffer)

    stats = {
        "original_lines": total_lines,
        "start_marker_line": start_idx + 1,
        "end_marker_line": end_idx + 1,
        "lines_before_start_removed": start_idx,
        "lines_after_end_removed": total_lines - 1 - end_idx,
        "boundary_markers_removed": 2 if remove_boundary_markers else 0,
        "timelapse_blocks_removed": timelapse_blocks_removed,
        "timelapse_lines_removed": timelapse_lines_removed,
        "cleaned_lines": len(cleaned),
    }

    return cleaned, stats


def clean_gcode_file(
    input_path: str,
    output_path: Optional[str] = None,
    remove_boundary_markers: bool = False,
    all_skippable: bool = False,
) -> Tuple[str, Dict[str, int]]:
    """
    Cleans a standalone G-code file and writes output to file.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    cleaned_lines, stats = clean_gcode(
        lines,
        remove_boundary_markers=remove_boundary_markers,
        all_skippable=all_skippable,
    )

    if not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_cleaned{ext}"

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        f.writelines(cleaned_lines)

    return output_path, stats


def clean_gcode_archive(
    archive_path: str,
    output_dest: Optional[str] = None,
    prefix_archive: bool = False,
    suffix_cleaned: bool = False,
    remove_boundary_markers: bool = False,
    all_skippable: bool = False,
) -> List[Tuple[str, str, Dict[str, int]]]:
    """
    Cleans all .gcode files inside a .gcode.3mf or .3mf zip archive
    (typically in the Metadata folder) and outputs the cleaned G-code
    files outside the zip, one for each plate.
    """
    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Archive file not found: {archive_path}")

    archive_dir = os.path.dirname(os.path.abspath(archive_path))
    archive_stem = os.path.basename(archive_path)
    if archive_stem.lower().endswith(".gcode.3mf"):
        archive_stem = archive_stem[:-10]
    else:
        archive_stem = os.path.splitext(archive_stem)[0]

    out_dir = archive_dir
    if output_dest:
        if os.path.isdir(output_dest) or output_dest.endswith(("/", "\\")):
            out_dir = output_dest
        else:
            _, ext = os.path.splitext(output_dest)
            if not ext or ext.lower() not in [".gcode"]:
                out_dir = output_dest

    os.makedirs(out_dir, exist_ok=True)

    results: List[Tuple[str, str, Dict[str, int]]] = []

    with zipfile.ZipFile(archive_path, "r") as z:
        names = z.namelist()
        plate_entries = [
            n for n in names
            if n.lower().startswith("metadata/") and n.lower().endswith(".gcode") and not n.endswith("/")
        ]
        if not plate_entries:
            plate_entries = [
                n for n in names
                if n.lower().endswith(".gcode") and not n.endswith("/")
            ]

        if not plate_entries:
            raise ValueError(f"No .gcode files found in archive: {archive_path}")

        plate_entries.sort(key=natural_sort_key)

        for entry in plate_entries:
            raw_content = z.read(entry).decode("utf-8", errors="ignore")
            lines = raw_content.splitlines(keepends=True)

            cleaned_lines, stats = clean_gcode(
                lines,
                remove_boundary_markers=remove_boundary_markers,
                all_skippable=all_skippable,
            )

            plate_base = os.path.basename(entry)
            plate_stem, plate_ext = os.path.splitext(plate_base)

            parts = []
            if prefix_archive:
                parts.append(archive_stem)
            parts.append(plate_stem)
            if suffix_cleaned:
                parts.append("cleaned")

            final_filename = "_".join(parts) + plate_ext
            target_path = os.path.join(out_dir, final_filename)

            with open(target_path, "w", encoding="utf-8", newline="") as f:
                f.writelines(cleaned_lines)

            results.append((entry, target_path, stats))

    return results


def clean_folder(
    folder_path: str,
    output_dest: Optional[str] = None,
    prefix_archive: bool = False,
    suffix_cleaned: bool = False,
    remove_boundary_markers: bool = False,
    all_skippable: bool = False,
) -> int:
    """
    Scans a directory and cleans all *.gcode.3mf and standalone *.gcode files found.
    """
    folder_path = os.path.abspath(folder_path)
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Folder not found: {folder_path}")

    all_entries = os.listdir(folder_path)

    # 1. Identify archives: *.gcode.3mf
    archive_files = [
        os.path.join(folder_path, f) for f in all_entries
        if f.lower().endswith(".gcode.3mf") and os.path.isfile(os.path.join(folder_path, f))
    ]
    archive_files.sort(key=natural_sort_key)

    extracted_plate_paths: Set[str] = set()
    total_processed = 0

    if archive_files:
        for arch in archive_files:
            print(f"\nProcessing archive: {os.path.basename(arch)}...")
            results = clean_gcode_archive(
                archive_path=arch,
                output_dest=output_dest or folder_path,
                prefix_archive=prefix_archive,
                suffix_cleaned=suffix_cleaned,
                remove_boundary_markers=remove_boundary_markers,
                all_skippable=all_skippable,
            )
            print_archive_summary(arch, results)
            for _, target_path, _ in results:
                extracted_plate_paths.add(os.path.abspath(target_path).lower())
            total_processed += 1

    # 2. Identify standalone *.gcode files
    # Exclude files ending with _cleaned.gcode and files that were just generated from archives
    gcode_files = [
        os.path.join(folder_path, f) for f in all_entries
        if f.lower().endswith(".gcode")
        and not f.lower().endswith(".gcode.3mf")
        and not f.lower().endswith("_cleaned.gcode")
        and os.path.abspath(os.path.join(folder_path, f)).lower() not in extracted_plate_paths
        and os.path.isfile(os.path.join(folder_path, f))
    ]
    gcode_files.sort(key=natural_sort_key)

    if gcode_files:
        for gfile in gcode_files:
            print(f"\nProcessing G-code file: {os.path.basename(gfile)}...")
            out_file = None
            if output_dest and os.path.isdir(output_dest):
                base, ext = os.path.splitext(os.path.basename(gfile))
                out_file = os.path.join(output_dest, f"{base}_cleaned{ext}")
            dest_path, stats = clean_gcode_file(
                input_path=gfile,
                output_path=out_file,
                remove_boundary_markers=remove_boundary_markers,
                all_skippable=all_skippable,
            )
            print_file_summary(gfile, dest_path, stats)
            total_processed += 1

    if total_processed == 0:
        print(f"No .gcode or .gcode.3mf files found to clean in: {folder_path}")

    return total_processed


def print_file_summary(input_path: str, output_path: str, stats: Dict[str, int]) -> None:
    print("=" * 60)
    print("G-CODE CLEANING SUMMARY")
    print("=" * 60)
    print(f"Input file:                    {input_path}")
    print(f"Output file:                   {output_path}")
    print(f"Original total lines:          {stats['original_lines']:,}")
    print(f"Start marker line:             Line {stats['start_marker_line']:,} (; MACHINE_START_GCODE_END)")
    print(f"Lines before start removed:    {stats['lines_before_start_removed']:,}")
    print(f"End marker line:               Line {stats['end_marker_line']:,} (; MACHINE_END_GCODE_START)")
    print(f"Lines after end removed:       {stats['lines_after_end_removed']:,}")
    print(f"Boundary markers removed:      {stats['boundary_markers_removed']}")
    print(f"Timelapse blocks removed:      {stats['timelapse_blocks_removed']} ({stats['timelapse_lines_removed']:,} lines)")
    total_removed = (
        stats["lines_before_start_removed"]
        + stats["lines_after_end_removed"]
        + stats["timelapse_lines_removed"]
        + stats["boundary_markers_removed"]
    )
    print(f"Total lines removed:           {total_removed:,}")
    print(f"Cleaned output lines:          {stats['cleaned_lines']:,}")
    print("=" * 60)


def print_archive_summary(archive_path: str, results: List[Tuple[str, str, Dict[str, int]]]) -> None:
    print("=" * 60)
    print(f"3MF ARCHIVE CLEANING SUMMARY: {archive_path}")
    print("=" * 60)
    print(f"Found {len(results)} plate(s):")
    for entry, out_path, _ in results:
        print(f"  - {entry} -> {out_path}")
    print()

    for idx, (entry, out_path, stats) in enumerate(results, start=1):
        print(f"--- Plate {idx}: {entry} -> {os.path.basename(out_path)} ---")
        print(f"Original total lines:          {stats['original_lines']:,}")
        print(f"Start marker line:             Line {stats['start_marker_line']:,}")
        print(f"Lines before start removed:    {stats['lines_before_start_removed']:,}")
        print(f"End marker line:               Line {stats['end_marker_line']:,}")
        print(f"Lines after end removed:       {stats['lines_after_end_removed']:,}")
        print(f"Timelapse blocks removed:      {stats['timelapse_blocks_removed']} ({stats['timelapse_lines_removed']:,} lines)")
        total_removed = (
            stats["lines_before_start_removed"]
            + stats["lines_after_end_removed"]
            + stats["timelapse_lines_removed"]
            + stats["boundary_markers_removed"]
        )
        print(f"Total lines removed:           {total_removed:,}")
        print(f"Cleaned output lines:          {stats['cleaned_lines']:,}")
        print()

    print("=" * 60)
    print(f"Successfully cleaned and exported {len(results)} plate file(s) outside the zip!")
    print("=" * 60)


def is_archive(path: str) -> bool:
    """Check if the given path is a zip / 3mf archive."""
    if not os.path.exists(path):
        return False
    lower = path.lower()
    if lower.endswith(".3mf") or lower.endswith(".zip"):
        return True
    return zipfile.is_zipfile(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove useless G-code from .gcode or .gcode.3mf archives (start routine, end routine, skippable timelapse)."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[],
        help="Path to input .gcode or .gcode.3mf file(s). If omitted, processes current folder.",
    )
    parser.add_argument(
        "--folder",
        "-f",
        default=None,
        help="Process all .gcode and .gcode.3mf files in the specified folder",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output destination path or directory",
    )
    parser.add_argument(
        "--in-place",
        "-i",
        action="store_true",
        help="Overwrite input file in-place (standalone .gcode only)",
    )
    parser.add_argument(
        "--prefix-archive",
        action="store_true",
        help="For archives, prefix output plate filenames with archive name (e.g. test_plate_1.gcode)",
    )
    parser.add_argument(
        "--suffix-cleaned",
        action="store_true",
        help="For archives, append '_cleaned' to plate filenames (e.g. plate_1_cleaned.gcode)",
    )
    parser.add_argument(
        "--remove-markers",
        action="store_true",
        help="Also remove the '; MACHINE_START_GCODE_END' and '; MACHINE_END_GCODE_START' marker lines themselves",
    )
    parser.add_argument(
        "--all-skippable",
        action="store_true",
        help="Remove all skippable blocks, not only those with SKIPTYPE: timelapse",
    )

    args = parser.parse_args()

    try:
        # Folder mode
        if args.folder:
            clean_folder(
                folder_path=args.folder,
                output_dest=args.output,
                prefix_archive=args.prefix_archive,
                suffix_cleaned=args.suffix_cleaned,
                remove_boundary_markers=args.remove_markers,
                all_skippable=args.all_skippable,
            )
            return 0

        # If no inputs provided, default to current folder
        if not args.inputs:
            clean_folder(
                folder_path=".",
                output_dest=args.output,
                prefix_archive=args.prefix_archive,
                suffix_cleaned=args.suffix_cleaned,
                remove_boundary_markers=args.remove_markers,
                all_skippable=args.all_skippable,
            )
            return 0

        # If single input that is a directory
        if len(args.inputs) == 1 and os.path.isdir(args.inputs[0]):
            clean_folder(
                folder_path=args.inputs[0],
                output_dest=args.output,
                prefix_archive=args.prefix_archive,
                suffix_cleaned=args.suffix_cleaned,
                remove_boundary_markers=args.remove_markers,
                all_skippable=args.all_skippable,
            )
            return 0

        # Process listed files
        for input_file in args.inputs:
            if is_archive(input_file):
                results = clean_gcode_archive(
                    archive_path=input_file,
                    output_dest=args.output,
                    prefix_archive=args.prefix_archive,
                    suffix_cleaned=args.suffix_cleaned,
                    remove_boundary_markers=args.remove_markers,
                    all_skippable=args.all_skippable,
                )
                print_archive_summary(input_file, results)
            else:
                output_file = input_file if args.in_place else args.output
                dest_path, stats = clean_gcode_file(
                    input_path=input_file,
                    output_path=output_file,
                    remove_boundary_markers=args.remove_markers,
                    all_skippable=args.all_skippable,
                )
                print_file_summary(input_file, dest_path, stats)

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
