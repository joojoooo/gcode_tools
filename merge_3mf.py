#!/usr/bin/env python3
"""
merge_3mf.py
============
Merges all .gcode.3mf files in the current folder (or a specified directory)
into 1 .gcode.3mf file with a single build plate containing the merged G-code
from all plates of all other 3MF files.

Key Features & Specifications:
-------------------------------
1. Ordering:
   Orders plates based on the source 3MF filenames using natural/alphanumeric
   sorting (e.g. '1_MagicOctagon' before '2_EthernetCover', '9_...' before '10_...').
   If a 3MF file has multiple plates (e.g. plate_1.gcode, plate_2.gcode), they
   are merged in sequential order.

2. Start & End G-Code:
   - Keeps only ONE machine start (MACHINE_START_GCODE) at the beginning of the
     print job (from the first plate).
   - Keeps only ONE machine end (MACHINE_END_GCODE) at the very end of the print job.
   - Eliminates redundant homing, heating, bed leveling, and vibration calibration.

3. Plate Swap & Pause (Bambu Lab M400 U1):
   - Uses Bambu Lab's native pause command ('M400 U1') between each plate's G-code.
   - Bambu firmware automatically handles retraction, safe Z-hop within machine limits,
     and parking over the waste chute / purge wiper pad.
   - Stepper motors remain energized (preserving homing and coordinates).
   - Target temperatures for the incoming plate are set BEFORE the pause via non-blocking
     M140 (bed) and M104 (nozzle). This ensures:
       a) The bed starts adjusting immediately while you physically swap plates.
       b) When you press Resume, the Bambu A1 firmware automatically reheats to the new
          plate's target temperature while still parked over the waste chute, and executes
          its native purge & silicone brush wipe at that temperature before leaving park!
       c) The nozzle never sits idle over the build plate waiting to reach temperature,
          preventing ooze or melted spots on the fresh plate.

4. Skip Parts Feature Support in Bambu Handy:
   - Merges top_*.png across all plates into Metadata/top_1.png so all objects are visible.
   - Merges pick_*.png across all plates into Metadata/pick_1.png so all objects can be
     interactively tapped and selected in Bambu Handy / on printer touchscreen.
   - Automatically resolves duplicate identify_id conflicts across different source files,
     remapping duplicates in G-code, slice_info.config, and pick_1.png.
   - Combines bbox_objects and computes unified bbox_all in Metadata/plate_1.json.

5. Unified Preview Collage:
   - Creates a multi-plate preview collage in Metadata/plate_1.png (512x512) and
     Metadata/plate_1_small.png (128x128), combining the 3D previews of all plates.
   - Also merges Metadata/plate_no_light_1.png.

6. Priming Line & Extrude Cali Test:
   - Pressure advance calibration (M900 S/C/R) and dynamic flow calibration
     only execute once at the beginning of the print.
   - The nozzle priming line is kept and executed before the start of EVERY new plate,
     re-pressurizing the nozzle and ensuring flawless first-layer adhesion.

7. Monotonic Layer & Progress Calculation on Screen:
   - Recalculates current layer numbers (M73 L{cur}, M991 S0 P{cur-1}, and
     '; layer num/total_layer_count: {cur}/{total}') monotonically across all plates.
   - Updates total layer count in headers and metadata.
   - Recalculates M73 P (percentage) and M73 R (remaining minutes) across the combined
     print time.

8. Pause Schedule & Timer Output:
   - Prints a detailed, clear schedule of when each pause and final finish will occur.
   - Provides both the relative timer to set right when clicking resume (from the previous
     pause) and cumulative print time from start, so plate swap delays don't throw off
     your timers.

9. MD5 Checksum & 3MF Metadata:
   - Calculates the exact uppercase 32-character MD5 checksum of the merged G-code
     and writes 'Metadata/plate_1.gcode.md5'.
   - Updates 'Metadata/slice_info.config', 'Metadata/plate_1.json', and
     'Metadata/model_settings.config' for a unified single-plate 3MF.
"""

import os
import sys
import re
import io
import math
import json
import zipfile
import hashlib
import argparse
import xml.etree.ElementTree as ET

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def natural_sort_key(s):
    """Sort strings containing numbers in human natural order (e.g. 1, 2, 10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]


def format_duration(seconds):
    """Format seconds into 'Xm Ys' or 'Xh Ym Zs'."""
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def format_timer(seconds):
    """Format seconds into a rounded timer string (e.g. '22m', '1h 12m')."""
    mins = int(round(seconds / 60.0))
    if mins < 1:
        mins = 1
    if mins < 60:
        return f"{mins}m"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m" if m > 0 else f"{h}h"


def parse_duration_str(s):
    """Parse '15m 19s' or '1h 2m 3s' into seconds."""
    total = 0
    h_m = re.search(r'(\d+)\s*h', s)
    m_m = re.search(r'(\d+)\s*m', s)
    s_m = re.search(r'(\d+)\s*s', s)
    if h_m:
        total += int(h_m.group(1)) * 3600
    if m_m:
        total += int(m_m.group(1)) * 60
    if s_m:
        total += int(s_m.group(1))
    return total


class PlateInfo:
    """Represents a single build plate extracted from a .gcode.3mf file."""

    def __init__(self, source_file, plate_index, gcode_text, json_data=None, slice_info_plate=None,
                 top_img=None, pick_img=None, preview_img=None, no_light_img=None, small_img=None):
        self.source_file = source_file
        self.plate_index = plate_index
        self.gcode_text = gcode_text
        self.json_data = json_data or {}
        self.slice_info_plate = slice_info_plate

        # Images (PIL.Image or None)
        self.top_image = top_img
        self.pick_image = pick_img
        self.preview_image = preview_img
        self.no_light_image = no_light_img
        self.small_image = small_img

        self.lines = gcode_text.splitlines()
        self._parse_metrics()
        self._parse_boundaries()

    def _parse_metrics(self):
        # Bed and nozzle temp defaults
        self.bed_temp = 45
        self.nozzle_temp = 220

        # 1. Search machine start G-code for the exact emitted temperatures
        lines = self.lines[:1200]
        found_bed_in_gcode = False
        found_nozzle_in_gcode = False

        for line in lines:
            if line.strip() == '; MACHINE_START_GCODE_END':
                break
            # Emitted bed temp: e.g. 'M190 S45; ensure bed temp'
            if 'ensure bed temp' in line or re.match(r'^\s*M190\s+S(\d+)', line):
                m = re.search(r'M190\s+S(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.bed_temp = int(m.group(1))
                    found_bed_in_gcode = True
            # Emitted nozzle initial temp: 'M104 S225 ; prepare to print' or 'M109 S225' in cali test
            if ('prepare to print' in line or 'extrude cali test' in line) and not found_nozzle_in_gcode:
                m = re.search(r'M10[49]\s+S(\d+)', line)
                if m and int(m.group(1)) > 150:
                    self.nozzle_temp = int(m.group(1))
                    found_nozzle_in_gcode = True

        # 2. Check config block comments at top of file
        bed_type = self.json_data.get('bed_type', '')
        bed_key = f'; {bed_type}_temp_initial_layer =' if bed_type else None

        for line in self.lines[:600]:
            if line.startswith('; nozzle_temperature_initial_layer ='):
                m = re.search(r'=\s*(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.nozzle_temp = int(m.group(1))
            elif bed_key and line.startswith(bed_key):
                m = re.search(r'=\s*(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.bed_temp = int(m.group(1))
            elif not found_bed_in_gcode and line.startswith('; supertack_plate_temp_initial_layer ='):
                m = re.search(r'=\s*(\d+)', line)
                if m and int(m.group(1)) > 0:
                    self.bed_temp = int(m.group(1))

        # Layer count
        self.layer_count = 0
        m = re.search(r'; total layer number:\s*(\d+)', self.gcode_text)
        if m:
            self.layer_count = int(m.group(1))
        else:
            layer_matches = re.findall(r'; layer num/total_layer_count:\s*(\d+)/', self.gcode_text)
            if layer_matches:
                self.layer_count = max(int(l) for l in layer_matches)
            else:
                self.layer_count = self.gcode_text.count('; CHANGE_LAYER')

        # Filament & timing metrics
        self.filament_length_mm = 0.0
        self.filament_volume_cm3 = 0.0
        self.filament_weight_g = 0.0
        self.max_z_height = 0.0
        self.printing_time_s = 0
        self.estimated_time_s = 0

        m = re.search(r'; total filament length \[mm\]\s*:\s*([\d.]+)', self.gcode_text)
        if m:
            self.filament_length_mm = float(m.group(1))
        m = re.search(r'; total filament volume \[cm\^3\]\s*:\s*([\d.]+)', self.gcode_text)
        if m:
            self.filament_volume_cm3 = float(m.group(1))
        m = re.search(r'; total filament weight \[g\]\s*:\s*([\d.]+)', self.gcode_text)
        if m:
            self.filament_weight_g = float(m.group(1))
        m = re.search(r'; max_z_height\s*:\s*([\d.]+)', self.gcode_text)
        if m:
            self.max_z_height = float(m.group(1))

        m = re.search(r'; model printing time:\s*([^;]+);\s*total estimated time:\s*([^\n\r]+)', self.gcode_text)
        if m:
            self.printing_time_s = parse_duration_str(m.group(1))
            self.estimated_time_s = parse_duration_str(m.group(2))
        elif self.slice_info_plate is not None:
            pred_meta = self.slice_info_plate.find("./metadata[@key='prediction']")
            if pred_meta is not None:
                self.estimated_time_s = int(pred_meta.attrib.get('value', 0))
                self.printing_time_s = int(self.estimated_time_s * 0.8)

        if self.estimated_time_s == 0:
            self.estimated_time_s = max(60, self.layer_count * 15)

        # Objects
        self.objects = []
        if self.slice_info_plate is not None:
            for obj in self.slice_info_plate.findall('object'):
                self.objects.append({
                    'identify_id': obj.attrib.get('identify_id', ''),
                    'name': obj.attrib.get('name', ''),
                    'skipped': obj.attrib.get('skipped', 'false')
                })

    def _parse_boundaries(self):
        self.exec_start_idx = None
        self.machine_start_end_idx = None
        self.first_change_layer_idx = None
        self.machine_end_start_idx = None
        self.cali_test_idx = None

        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped == '; EXECUTABLE_BLOCK_START':
                self.exec_start_idx = i
            elif stripped == '; MACHINE_START_GCODE_END':
                self.machine_start_end_idx = i
            elif stripped == '; CHANGE_LAYER' and self.first_change_layer_idx is None and self.machine_start_end_idx is not None:
                self.first_change_layer_idx = i
            elif stripped == '; MACHINE_END_GCODE_START':
                self.machine_end_start_idx = i
            elif self.exec_start_idx is not None and ';===== extrude cali test =====' in line and self.cali_test_idx is None:
                self.cali_test_idx = i

        # Fallbacks
        if self.exec_start_idx is None:
            self.exec_start_idx = 0
        if self.machine_start_end_idx is None:
            for i, line in enumerate(self.lines):
                if line.strip() == '; CHANGE_LAYER':
                    self.first_change_layer_idx = i
                    self.machine_start_end_idx = max(0, i - 1)
                    break
        if self.first_change_layer_idx is None:
            self.first_change_layer_idx = self.machine_start_end_idx + 1 if self.machine_start_end_idx else 0
        if self.machine_end_start_idx is None:
            for i in range(len(self.lines) - 1, -1, -1):
                if 'M140 S0' in self.lines[i] or 'M18' in self.lines[i]:
                    self.machine_end_start_idx = max(0, i - 10)
                    break
            if self.machine_end_start_idx is None:
                self.machine_end_start_idx = len(self.lines) - 1

    def remap_object_id(self, old_id_str, new_id_str):
        """Remap an object ID in G-code, metadata, and pick_image to resolve collisions."""
        old_id = int(old_id_str)
        new_id = int(new_id_str)

        # Update in objects list
        for obj in self.objects:
            if obj['identify_id'] == old_id_str:
                obj['identify_id'] = new_id_str

        # Update in G-code text and lines
        pattern = re.compile(rf'(; OBJECT_ID:\s*){old_id}\b')
        new_lines = []
        for line in self.lines:
            new_lines.append(pattern.sub(rf'\g<1>{new_id}', line))
        self.lines = new_lines
        self.gcode_text = "\n".join(new_lines)

        # Remap in pick_image if present
        if self.pick_image is not None and HAS_PIL:
            data = bytearray(self.pick_image.tobytes())
            for i in range(0, len(data), 4):
                if data[i] == old_id and data[i+3] > 0:
                    data[i] = new_id
            self.pick_image = Image.frombytes("RGBA", self.pick_image.size, bytes(data))

    def extract_priming_line(self):
        """
        Extracts the priming line moves from the plate's extrude cali test section,
        stripping out pressure advance calibration instructions (M900 S/C/R)
        and dynamic flow calibration flag checks.
        """
        prime_lines = []
        if self.cali_test_idx is not None:
            # 1. Backwards search for the initial position setup before cali test
            # (e.g. G1 X108.000 Y-0.500 F30000, G1 Z0.300 F1200, M400, G2814 Z0.32)
            pos_lines = []
            for i in range(self.cali_test_idx - 1, max(self.exec_start_idx, self.cali_test_idx - 35), -1):
                l = self.lines[i].strip()
                if l.startswith('G1 X') or l.startswith('G0 X') or l.startswith('G1 Z') or l.startswith('G2814') or l.startswith('M400'):
                    pos_lines.insert(0, self.lines[i])
                    if 'Y-0.5' in l or 'Y0' in l or 'Y1' in l:
                        break

            # 2. Extract the actual prime line extrusion from cali test
            cali_moves = []
            in_flag_block = False
            for i in range(self.cali_test_idx + 1, min(len(self.lines), self.cali_test_idx + 100)):
                line = self.lines[i]
                stripped = line.strip()
                if '; MACHINE_START_GCODE_END' in stripped or ';========turn off light' in stripped:
                    break
                if 'extrude_cali_flag' in stripped:
                    in_flag_block = True
                    continue
                if in_flag_block:
                    if 'M623' in stripped:
                        in_flag_block = False
                    continue
                if stripped.startswith('M900'):
                    continue
                if stripped.startswith(';'):
                    continue
                if stripped:
                    cali_moves.append(line)

            prime_lines.extend(pos_lines)
            prime_lines.extend(cali_moves)

        # Fallback if no cali moves were extracted
        if not prime_lines:
            prime_lines = [
                "G90",
                "G1 Z5 F1200",
                "G1 X108.000 Y-0.500 F30000",
                "G1 Z0.300 F1200",
                "M400",
                f"M104 S{self.nozzle_temp} ; ensure nozzle temp",
                "G90",
                "M83",
                "G0 X128 E8 F720",
                "G0 X133 E.3742 F1200",
                "G0 X138 E.3742 F4800",
                "G0 X143 E.3742 F1200",
                "G0 X148 E.3742 F4800",
                "G0 X153 E.3742 F1200",
                "G91",
                "G1 X1 Z-0.300",
                "G1 X4",
                "G1 Z1 F1200",
                "G90",
                "M400",
                "G1 Z0.2"
            ]

        # Post-prime wiping & detector setup
        post_prime = []
        if not (prime_lines and prime_lines[-1].strip().startswith("G1 Z")):
            post_prime.append("G1 Z0.2")

        post_prime.extend([
            "M975 S1 ; turn on mech mode suppression",
            "G90",
            "G21",
            "M83",
            "T1000",
            "M211 X0 Y0 Z0 ; turn off soft endstops",
            "M1007 S1 ; turn on mass estimation",
            "G29.4",
            "M981 S1 P20000 ; open spaghetti detector"
        ])

        return prime_lines + post_prime


def make_preview_collage(images, canvas_size=(512, 512)):
    """Creates a clean multi-plate collage for 3MF preview images."""
    if not HAS_PIL or not images:
        return None
    if len(images) == 1:
        return images[0].resize(canvas_size, Image.Resampling.LANCZOS)

    n = len(images)
    if n <= 2:
        cols, rows = 2, 1
    elif n <= 4:
        cols, rows = 2, 2
    elif n <= 6:
        cols, rows = 3, 2
    else:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

    canvas_w, canvas_h = canvas_size
    cell_w = canvas_w // cols
    cell_h = canvas_h // rows

    collage = Image.new('RGBA', canvas_size, (0, 0, 0, 0))
    for idx, img in enumerate(images):
        col = idx % cols
        row = idx // cols
        x_offset = col * cell_w
        y_offset = row * cell_h

        w, h = img.size
        scale = min(cell_w / w, cell_h / h)
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        paste_x = x_offset + (cell_w - new_w) // 2
        paste_y = y_offset + (cell_h - new_h) // 2
        collage.alpha_composite(resized, (paste_x, paste_y))

    return collage


def merge_plates(plates, pause_gcode="M400 U1", z_lift=0.0):
    """
    Combines G-code from multiple PlateInfo objects into a single G-code string.
    Recalculates layer numbers, totals, progress (M73 P/R), and updates headers.
    """
    if not plates:
        raise ValueError("No plates provided to merge.")

    total_layers = sum(p.layer_count for p in plates)
    total_estimated_s = sum(p.estimated_time_s for p in plates)
    total_printing_s = sum(p.printing_time_s for p in plates)
    total_filament_mm = sum(p.filament_length_mm for p in plates)
    total_filament_cm3 = sum(p.filament_volume_cm3 for p in plates)
    total_filament_g = sum(p.filament_weight_g for p in plates)
    max_z = max(p.max_z_height for p in plates)

    merged_lines = []

    # 1. PLATE 1 START:
    # Scale M73 P/R in Plate 1 machine start so progress counts down across total job
    plate1 = plates[0]
    p1_duration_s = max(1, plate1.estimated_time_s)

    for line in plate1.lines[:plate1.first_change_layer_idx]:
        m = re.match(r'^M73\s+P(\d+)\s+R(\d+)(.*)$', line)
        if m:
            orig_r = int(m.group(2))
            elapsed_in_p1_s = max(0, p1_duration_s - (orig_r * 60))
            new_r = max(0, int(round((total_estimated_s - elapsed_in_p1_s) / 60.0)))
            new_p = min(99, max(0, int(round((elapsed_in_p1_s / float(total_estimated_s)) * 100.0)))) if total_estimated_s > 0 else 0
            merged_lines.append(f"M73 P{new_p} R{new_r}{m.group(3)}")
            continue
        merged_lines.append(line)

    # 2. PROCESS EACH PLATE BODY
    layer_offset = 0
    time_offset_s = 0

    for idx, plate in enumerate(plates):
        is_first = (idx == 0)

        if not is_first:
            # Transition block between plates
            merged_lines.append("")
            merged_lines.append("; ====================================================================")
            merged_lines.append(f"; ==================== PLATE TRANSITION: {idx} -> {idx+1} ===================")
            merged_lines.append(f"; Next Plate: {os.path.basename(plate.source_file)} (Plate {plate.plate_index})")
            merged_lines.append("; ====================================================================")
            merged_lines.append("M400 ; wait for motion buffer to clear")
            merged_lines.append("M106 S0 ; turn off part cooling fan")
            merged_lines.append("M981 S0 P20000 ; close spaghetti detector")

            # PRE-PAUSE TEMPERATURE PREPARATION:
            # Setting target temps BEFORE M400 U1 ensures:
            # 1. The bed starts warming/cooling to the new plate's temp while user swaps plates.
            # 2. When the user hits Resume, the printer's firmware reheats to this target temp
            #    WHILE PARKED at the waste chute/wiper, purges, and wipes clean BEFORE moving
            #    out over the build plate. No nozzle dwelling or oozing over the bed!
            merged_lines.append("; Pre-set next plate temperatures so firmware uses them during resume wipe")
            merged_lines.append(f"M140 S{plate.bed_temp} ; set bed temperature (async)")
            merged_lines.append(f"M104 S{plate.nozzle_temp} ; set nozzle temperature (async)")

            # Optional extra manual Z-lift if explicitly requested (defaults to 0, letting M400 U1 handle Z-hop)
            if z_lift > 0:
                merged_lines.append("G91")
                merged_lines.append(f"G1 Z{z_lift:.2f} F1200 ; optional manual Z lift")
                merged_lines.append("G90")

            merged_lines.append("; --- PAUSE FOR PLATE REPLACEMENT ---")
            merged_lines.append(f"{pause_gcode} ; PAUSE: Switch build plate, place clean plate, and click Resume")
            merged_lines.append("")

            # POST-RESUME SETUP:
            merged_lines.append("; --- POST-RESUME: ENSURE BED TEMP & PRINT PRIMING LINE ---")
            merged_lines.append("M400")
            merged_lines.append("G90")
            merged_lines.append("M83 ; relative extrusion")
            # Wait for bed temp while nozzle is still floating safely up at return coordinates
            merged_lines.append(f"M190 S{plate.bed_temp} ; ensure bed temperature before descending to plate")
            merged_lines.append(f"M104 S{plate.nozzle_temp} ; ensure nozzle temperature is active")
            merged_lines.append("")
            merged_lines.append("; Priming line for new plate")

            # Add priming line
            prime_code = plate.extract_priming_line()
            merged_lines.extend(prime_code)
            merged_lines.append("; ==================== BEGIN PRINTING PLATE OBJECTS =================")
            merged_lines.append("")

        # Extract print body lines
        body_lines = plate.lines[plate.first_change_layer_idx : plate.machine_end_start_idx]
        plate_duration_s = max(1, plate.estimated_time_s)

        for line in body_lines:
            # Adjust '; layer num/total_layer_count: cur/orig_total'
            m = re.match(r'^(;\s*layer num/total_layer_count:\s*)(\d+)/(\d+)(.*)$', line)
            if m:
                cur_layer = int(m.group(2))
                new_layer = cur_layer + layer_offset
                merged_lines.append(f"{m.group(1)}{new_layer}/{total_layers}{m.group(4)}")
                continue

            # Adjust 'M73 L{cur}'
            m = re.match(r'^(M73\s+L)(\d+)(.*)$', line)
            if m:
                cur_layer = int(m.group(2))
                new_layer = cur_layer + layer_offset
                merged_lines.append(f"{m.group(1)}{new_layer}{m.group(3)}")
                continue

            # Adjust 'M991 S0 P{cur-1}'
            m = re.match(r'^(M991\s+S0\s+P)(\d+)(.*)$', line)
            if m:
                cur_p = int(m.group(2))
                new_p = cur_p + layer_offset
                merged_lines.append(f"{m.group(1)}{new_p}{m.group(3)}")
                continue

            # Adjust 'M73 P{percent} R{minutes_remaining}'
            m = re.match(r'^M73\s+P(\d+)\s+R(\d+)(.*)$', line)
            if m:
                orig_r = int(m.group(2))
                elapsed_in_plate_s = max(0, plate_duration_s - (orig_r * 60))
                total_elapsed_s = time_offset_s + elapsed_in_plate_s
                new_r = max(0, int(round((total_estimated_s - total_elapsed_s) / 60.0)))
                new_p = min(99, max(0, int(round((total_elapsed_s / float(total_estimated_s)) * 100.0)))) if total_estimated_s > 0 else 0
                merged_lines.append(f"M73 P{new_p} R{new_r}{m.group(3)}")
                continue

            merged_lines.append(line)

        layer_offset += plate.layer_count
        time_offset_s += plate.estimated_time_s

    # 3. MACHINE END GCODE (from the last plate)
    last_plate = plates[-1]
    merged_lines.extend(last_plate.lines[last_plate.machine_end_start_idx:])

    # 4. UPDATE HEADER BLOCK AT THE TOP
    updated_lines = []
    in_header = False

    all_obj_labels = []
    for p in plates:
        for obj in p.objects:
            if obj['identify_id'] and obj['identify_id'] not in all_obj_labels:
                all_obj_labels.append(obj['identify_id'])

    for line in merged_lines:
        if line.strip() == '; HEADER_BLOCK_START':
            in_header = True
            updated_lines.append(line)
            continue
        if line.strip() == '; HEADER_BLOCK_END':
            in_header = False
            updated_lines.append(line)
            continue

        if in_header:
            if line.startswith('; total layer number:'):
                updated_lines.append(f"; total layer number: {total_layers}")
                continue
            if line.startswith('; model printing time:'):
                updated_lines.append(f"; model printing time: {format_duration(total_printing_s)}; total estimated time: {format_duration(total_estimated_s)}")
                continue
            if line.startswith('; total filament length [mm] :'):
                updated_lines.append(f"; total filament length [mm] : {total_filament_mm:.2f}")
                continue
            if line.startswith('; total filament volume [cm^3] :'):
                updated_lines.append(f"; total filament volume [cm^3] : {total_filament_cm3:.2f}")
                continue
            if line.startswith('; total filament weight [g] :'):
                updated_lines.append(f"; total filament weight [g] : {total_filament_g:.2f}")
                continue
            if line.startswith('; max_z_height:'):
                updated_lines.append(f"; max_z_height: {max_z:.2f}")
                continue
            if line.startswith('; model label id:'):
                if all_obj_labels:
                    updated_lines.append(f"; model label id: {','.join(all_obj_labels)}")
                continue

        updated_lines.append(line)

    return "\n".join(updated_lines) + "\n"


def print_pause_schedule(plates):
    """Prints a clear schedule of when pauses and completion will occur for easy timer setting."""
    if len(plates) <= 1:
        return

    total_layers = sum(p.layer_count for p in plates)
    total_time_s = sum(p.estimated_time_s for p in plates)

    print("\n" + "=" * 68)
    print("                    SCHEDULED PLATE SWAP PAUSES")
    print("=" * 68)
    print(f" Total Plates: {len(plates)} | Total Layers: {total_layers} | Total Est. Print Time: ~{format_duration(total_time_s)}")
    print("-" * 68)

    cum_layers = 0
    cum_time_s = 0

    for i in range(len(plates) - 1):
        p_curr = plates[i]
        p_next = plates[i+1]
        cum_layers += p_curr.layer_count
        cum_time_s += p_curr.estimated_time_s

        plate_timer = format_timer(p_curr.estimated_time_s)
        curr_names = ", ".join(f"'{o['name']}'" for o in p_curr.objects) if p_curr.objects else f"Plate {p_curr.plate_index}"
        next_names = ", ".join(f"'{o['name']}'" for o in p_next.objects) if p_next.objects else f"Plate {p_next.plate_index}"

        print(f" [Pause #{i+1}]")
        print(f"   * Trigger Layer       : Layer {cum_layers} of {total_layers}")
        if i == 0:
            print(f"   * Timer (from Start)  : [Set Timer for: {plate_timer}]  <-- Set when print starts")
            print(f"   * Plate Print Time    : ~{format_duration(p_curr.estimated_time_s)}")
            print(f"   * Total Print Time    : ~{format_duration(cum_time_s)} from start")
        else:
            print(f"   * Timer (from Resume) : [Set Timer for: {plate_timer}]  <-- Set right when you resume Pause #{i}")
            print(f"   * Plate Print Time    : ~{format_duration(p_curr.estimated_time_s)}")
            print(f"   * Total Print Time    : ~{format_duration(cum_time_s)} from start (cumulative print time)")
        print(f"   * Completed Plate     : {os.path.basename(p_curr.source_file)} [Plate {p_curr.plate_index}] ({curr_names})")
        print(f"   * Action on Pause     : Remove plate -> Place clean plate for {os.path.basename(p_next.source_file)} [Plate {p_next.plate_index}] ({next_names})")
        print()

    # Final Plate Completion
    p_last = plates[-1]
    last_timer = format_timer(p_last.estimated_time_s)
    last_names = ", ".join(f"'{o['name']}'" for o in p_last.objects) if p_last.objects else f"Plate {p_last.plate_index}"

    print(f" [Print Completion]")
    print(f"   * Final Layer         : Layer {total_layers} of {total_layers}")
    print(f"   * Timer (from Resume) : [Set Timer for: {last_timer}]  <-- Set right when you resume Pause #{len(plates)-1}")
    print(f"   * Plate Print Time    : ~{format_duration(p_last.estimated_time_s)}")
    print(f"   * Total Print Time    : ~{format_duration(total_time_s)} from start")
    print(f"   * Completed Plate     : {os.path.basename(p_last.source_file)} [Plate {p_last.plate_index}] ({last_names})")
    print(f"   * Action on Finish    : Print Complete! Remove final plate from printer.")
    print("=" * 68 + "\n")


def merge_3mf_files(input_files, output_path, pause_gcode="M400 U1", z_lift=0.0, verbose=True):
    """
    Main merging function.
    Reads input .gcode.3mf files, parses all plates, merges G-code, images,
    and metadata, and writes out the combined .gcode.3mf file.
    """
    if verbose:
        print(f"[*] Found {len(input_files)} source .gcode.3mf file(s):")
        for i, f in enumerate(input_files, 1):
            print(f"    {i}. {os.path.basename(f)}")

    all_plates = []
    base_zip_path = input_files[0]
    used_object_ids = set()

    for fpath in input_files:
        with zipfile.ZipFile(fpath, 'r') as z:
            names = z.namelist()

            plate_gcode_names = [n for n in names if re.match(r'^Metadata/plate_\d+\.gcode$', n)]
            if not plate_gcode_names:
                plate_gcode_names = [n for n in names if n.endswith('.gcode')]

            plate_gcode_names.sort(key=natural_sort_key)

            slice_info_root = None
            if 'Metadata/slice_info.config' in names:
                try:
                    slice_info_root = ET.fromstring(z.read('Metadata/slice_info.config').decode('utf-8'))
                except Exception:
                    pass

            for gcode_name in plate_gcode_names:
                m = re.search(r'plate_(\d+)', gcode_name)
                plate_idx = int(m.group(1)) if m else 1

                gcode_text = z.read(gcode_name).decode('utf-8', errors='ignore')

                json_name = f"Metadata/plate_{plate_idx}.json"
                json_data = {}
                if json_name in names:
                    try:
                        json_data = json.loads(z.read(json_name).decode('utf-8'))
                    except Exception:
                        pass

                plate_elem = None
                if slice_info_root is not None:
                    for p in slice_info_root.findall('plate'):
                        idx_meta = p.find("./metadata[@key='index']")
                        if idx_meta is not None and int(idx_meta.attrib.get('value', -1)) == plate_idx:
                            plate_elem = p
                            break
                    if plate_elem is None:
                        plates_list = slice_info_root.findall('plate')
                        if plates_list:
                            plate_elem = plates_list[0]

                # Load images if Pillow is available
                top_img = None
                pick_img = None
                preview_img = None
                no_light_img = None
                small_img = None

                if HAS_PIL:
                    top_name = f"Metadata/top_{plate_idx}.png"
                    pick_name = f"Metadata/pick_{plate_idx}.png"
                    preview_name = f"Metadata/plate_{plate_idx}.png"
                    no_light_name = f"Metadata/plate_no_light_{plate_idx}.png"
                    small_name = f"Metadata/plate_{plate_idx}_small.png"

                    if top_name in names:
                        top_img = Image.open(io.BytesIO(z.read(top_name)))
                    if pick_name in names:
                        pick_img = Image.open(io.BytesIO(z.read(pick_name)))
                    if preview_name in names:
                        preview_img = Image.open(io.BytesIO(z.read(preview_name)))
                    if no_light_name in names:
                        no_light_img = Image.open(io.BytesIO(z.read(no_light_name)))
                    if small_name in names:
                        small_img = Image.open(io.BytesIO(z.read(small_name)))

                pinfo = PlateInfo(
                    source_file=fpath,
                    plate_index=plate_idx,
                    gcode_text=gcode_text,
                    json_data=json_data,
                    slice_info_plate=plate_elem,
                    top_img=top_img,
                    pick_img=pick_img,
                    preview_img=preview_img,
                    no_light_img=no_light_img,
                    small_img=small_img
                )

                # Check and resolve any object identify_id collisions
                for obj in pinfo.objects:
                    orig_id = obj['identify_id']
                    if orig_id:
                        if orig_id in used_object_ids:
                            # Find next unused integer ID between 100 and 250
                            new_int = 100
                            while str(new_int) in used_object_ids:
                                new_int += 1
                            new_id_str = str(new_int)
                            if verbose:
                                print(f"    [!] Remapping conflicting object ID '{orig_id}' -> '{new_id_str}' for '{obj['name']}' in {os.path.basename(fpath)} [P{plate_idx}]")
                            pinfo.remap_object_id(orig_id, new_id_str)
                            used_object_ids.add(new_id_str)
                        else:
                            used_object_ids.add(orig_id)

                all_plates.append(pinfo)
                if verbose:
                    print(f"    -> Loaded {os.path.basename(fpath)} [Plate {plate_idx}]: {pinfo.layer_count} layers, ~{format_duration(pinfo.estimated_time_s)}, bed={pinfo.bed_temp}C, nozzle={pinfo.nozzle_temp}C")

    if not all_plates:
        raise RuntimeError("No plates or G-code found in provided 3MF files.")

    # Print the scheduled pause timetable
    if verbose:
        print_pause_schedule(all_plates)
        print(f"[*] Merging {len(all_plates)} plate(s) into single G-code...")

    merged_gcode = merge_plates(all_plates, pause_gcode=pause_gcode, z_lift=z_lift)
    merged_gcode_bytes = merged_gcode.encode('utf-8')
    merged_md5 = hashlib.md5(merged_gcode_bytes).hexdigest().upper()

    total_layers = sum(p.layer_count for p in all_plates)
    total_estimated_s = sum(p.estimated_time_s for p in all_plates)
    total_weight_g = sum(p.filament_weight_g for p in all_plates)
    total_filament_m = sum(p.filament_length_mm for p in all_plates) / 1000.0

    if verbose:
        print(f"[*] Merged G-code created:")
        print(f"    - Total Layers: {total_layers}")
        print(f"    - Estimated Time: {format_duration(total_estimated_s)}")
        print(f"    - Filament: {total_filament_m:.2f} m ({total_weight_g:.2f} g)")
        print(f"    - Plate Pauses: {len(all_plates) - 1}")
        print(f"    - MD5 Checksum: {merged_md5}")

    with zipfile.ZipFile(base_zip_path, 'r') as z_base:
        base_files = {name: z_base.read(name) for name in z_base.namelist()}

    # Merge top_*.png and pick_*.png for Bambu Handy Skip Parts
    if HAS_PIL:
        if verbose:
            print("[*] Merging images for Bambu Handy skip parts & plate previews...")

        # 1. Merge top_*.png
        top_imgs = [p.top_image for p in all_plates if p.top_image is not None]
        if top_imgs:
            merged_top = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
            for t in top_imgs:
                merged_top.alpha_composite(t)
            top_buf = io.BytesIO()
            merged_top.save(top_buf, format='PNG')
            base_files['Metadata/top_1.png'] = top_buf.getvalue()

        # 2. Merge pick_*.png (hit-test mask for skip parts)
        pick_imgs = [p.pick_image for p in all_plates if p.pick_image is not None]
        if pick_imgs:
            merged_pick = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
            for pk in pick_imgs:
                merged_pick.paste(pk, (0, 0), mask=pk)
            pick_buf = io.BytesIO()
            merged_pick.save(pick_buf, format='PNG')
            base_files['Metadata/pick_1.png'] = pick_buf.getvalue()

        # 3. Merge preview images (plate_1.png, plate_1_small.png, plate_no_light_1.png)
        prev_imgs = [p.preview_image for p in all_plates if p.preview_image is not None]
        if prev_imgs:
            collage_prev = make_preview_collage(prev_imgs, (512, 512))
            if collage_prev:
                p_buf = io.BytesIO()
                collage_prev.save(p_buf, format='PNG')
                base_files['Metadata/plate_1.png'] = p_buf.getvalue()

                # Generate 128x128 small thumbnail
                small_buf = io.BytesIO()
                small_img = collage_prev.resize((128, 128), Image.Resampling.LANCZOS)
                small_img.save(small_buf, format='PNG')
                base_files['Metadata/plate_1_small.png'] = small_buf.getvalue()

        no_light_imgs = [p.no_light_image for p in all_plates if p.no_light_image is not None]
        if no_light_imgs:
            collage_no_light = make_preview_collage(no_light_imgs, (512, 512))
            if collage_no_light:
                nl_buf = io.BytesIO()
                collage_no_light.save(nl_buf, format='PNG')
                base_files['Metadata/plate_no_light_1.png'] = nl_buf.getvalue()

    # Update slice_info.config
    if 'Metadata/slice_info.config' in base_files:
        try:
            root = ET.fromstring(base_files['Metadata/slice_info.config'].decode('utf-8'))
            plate_elem = root.find('plate')
            if plate_elem is not None:
                for meta in plate_elem.findall('metadata'):
                    k = meta.attrib.get('key')
                    if k == 'prediction':
                        meta.attrib['value'] = str(total_estimated_s)
                    elif k == 'weight':
                        meta.attrib['value'] = f"{total_weight_g:.2f}"
                    elif k == 'pause_count':
                        meta.attrib['value'] = str(len(all_plates) - 1)
                    elif k == 'timelapse_type':
                        meta.attrib['value'] = "-1"
                    elif k == 'label_object_enabled':
                        meta.attrib['value'] = "true"

                for old_obj in plate_elem.findall('object'):
                    plate_elem.remove(old_obj)

                added_ids = set()
                for p in all_plates:
                    for obj in p.objects:
                        obj_id = obj['identify_id']
                        if obj_id and obj_id not in added_ids:
                            added_ids.add(obj_id)
                            new_obj = ET.Element('object', {
                                'identify_id': obj_id,
                                'name': obj['name'] or f"object_{obj_id}",
                                'skipped': obj.get('skipped', 'false')
                            })
                            plate_elem.append(new_obj)

                fil_elem = plate_elem.find('filament')
                if fil_elem is not None:
                    fil_elem.attrib['used_m'] = f"{total_filament_m:.2f}"
                    fil_elem.attrib['used_g'] = f"{total_weight_g:.2f}"

                layer_list = plate_elem.find('.//layer_filament_list')
                if layer_list is not None:
                    layer_list.attrib['layer_ranges'] = f"0 {total_layers - 1}"

            if hasattr(ET, 'indent'):
                ET.indent(root, space="  ")
            base_files['Metadata/slice_info.config'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        except Exception as e:
            if verbose:
                print(f"[!] Warning: slice_info.config update: {e}")

    # Update Metadata/plate_1.json
    all_bbox_objects = []
    min_x, min_y, max_x, max_y = 9999.0, 9999.0, -9999.0, -9999.0
    for p in all_plates:
        if 'bbox_objects' in p.json_data:
            for b_obj in p.json_data['bbox_objects']:
                all_bbox_objects.append(b_obj)
                b = b_obj.get('bbox', [])
                if len(b) == 4:
                    min_x = min(min_x, b[0])
                    min_y = min(min_y, b[1])
                    max_x = max(max_x, b[2])
                    max_y = max(max_y, b[3])

    plate_json_data = all_plates[0].json_data.copy() if all_plates[0].json_data else {}
    if all_bbox_objects:
        plate_json_data['bbox_objects'] = all_bbox_objects
        if min_x < max_x and min_y < max_y:
            plate_json_data['bbox_all'] = [min_x, min_y, max_x, max_y]
    plate_json_data['is_seq_print'] = True
    base_files['Metadata/plate_1.json'] = json.dumps(plate_json_data, indent=2).encode('utf-8')

    # Update model_settings.config to have only 1 plate
    if 'Metadata/model_settings.config' in base_files:
        try:
            m_root = ET.fromstring(base_files['Metadata/model_settings.config'].decode('utf-8'))
            plates_nodes = m_root.findall('plate')
            if len(plates_nodes) > 1:
                for extra in plates_nodes[1:]:
                    m_root.remove(extra)
            if hasattr(ET, 'indent'):
                ET.indent(m_root, space="  ")
            base_files['Metadata/model_settings.config'] = ET.tostring(m_root, encoding='utf-8', xml_declaration=True)
        except Exception:
            pass

    # Update cut_information.xml
    if 'Metadata/cut_information.xml' in base_files:
        try:
            c_root = ET.fromstring(base_files['Metadata/cut_information.xml'].decode('utf-8'))
            for existing_obj in list(c_root.findall('object')):
                c_root.remove(existing_obj)
            for i in range(1, len(all_plates) + 1):
                obj_tag = ET.Element('object', {'id': str(i)})
                cut_tag = ET.Element('cut_id', {'id': '0', 'check_sum': '1', 'connectors_cnt': '0'})
                obj_tag.append(cut_tag)
                c_root.append(obj_tag)
            if hasattr(ET, 'indent'):
                ET.indent(c_root, space=" ")
            base_files['Metadata/cut_information.xml'] = ET.tostring(c_root, encoding='utf-8', xml_declaration=True)
        except Exception:
            pass

    # Clean up any extra plate files from multi-plate base files
    keys_to_delete = [k for k in base_files if re.match(r'^Metadata/plate_\d+\.gcode.*$', k) and not k.startswith('Metadata/plate_1.gcode')]
    keys_to_delete += [k for k in base_files if re.match(r'^Metadata/plate_\d+\.json$', k) and k != 'Metadata/plate_1.json']
    keys_to_delete += [k for k in base_files if re.match(r'^Metadata/(?:top|pick|plate|plate_no_light)_\d+\.png$', k) and not k.endswith('_1.png') and not k.endswith('_1_small.png')]
    for k in keys_to_delete:
        del base_files[k]

    # Set new merged gcode and md5
    base_files['Metadata/plate_1.gcode'] = merged_gcode_bytes
    base_files['Metadata/plate_1.gcode.md5'] = (merged_md5 + "\n").encode('utf-8')

    # Write output .gcode.3mf archive
    if verbose:
        print(f"[*] Writing output archive to: {output_path}")

    with zipfile.ZipFile(output_path, 'w', compression=zipfile.ZIP_DEFLATED) as z_out:
        for fname, content in base_files.items():
            z_out.writestr(fname, content)

    if verbose:
        print(f"[+] Successfully merged {len(all_plates)} plates into: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Merge all .gcode.3mf files in a folder into 1 .gcode.3mf file with a single plate."
    )
    parser.add_argument(
        "-d", "--dir",
        default=".",
        help="Directory containing .gcode.3mf files (defaults to current directory)."
    )
    parser.add_argument(
        "-o", "--output",
        default="merged.gcode.3mf",
        help="Output .gcode.3mf file path (defaults to 'merged.gcode.3mf')."
    )
    parser.add_argument(
        "--pause-gcode",
        default="M400 U1",
        help="Pause G-code instruction between plates (defaults to 'M400 U1')."
    )
    parser.add_argument(
        "--z-lift",
        type=float,
        default=0.0,
        help="Optional extra manual Z-lift distance in mm before pause (defaults to 0.0 mm; M400 U1 natively handles Z-hop and parking)."
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress informational output."
    )

    args = parser.parse_args()

    target_dir = os.path.abspath(args.dir)
    output_path = os.path.abspath(args.output) if os.path.isabs(args.output) else os.path.abspath(os.path.join(target_dir, args.output))

    # Find all .gcode.3mf files in target directory
    all_3mf = [
        os.path.join(target_dir, f) for f in os.listdir(target_dir)
        if f.lower().endswith('.gcode.3mf')
    ]

    # Exclude output file and any file starting with 'merged'
    input_files = [
        f for f in all_3mf
        if os.path.abspath(f) != output_path and not os.path.basename(f).lower().startswith('merged')
    ]

    input_files.sort(key=natural_sort_key)

    if not input_files:
        print(f"[!] Error: No input .gcode.3mf files found in {target_dir}")
        sys.exit(1)

    try:
        merge_3mf_files(
            input_files=input_files,
            output_path=output_path,
            pause_gcode=args.pause_gcode,
            z_lift=args.z_lift,
            verbose=not args.quiet
        )
    except Exception as e:
        print(f"[!] Error merging files: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
