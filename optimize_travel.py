#!/usr/bin/env python3
"""
OrcaSlicer / Bambu Studio Travel Optimization Post-Processing Script (v5.0)
===========================================================================
Eliminates unnecessary diagonal "X" travel moves across open walls while
strictly preventing all forms of print corruption:
1. Exact Retraction & Wipe Ordering:
   - Pre-wipe retractions (e.g., G1 E-0.13333 F1800) remain strictly BEFORE ; WIPE_START.
   - Wipe-with-retraction (e.g., G1 X... Y... E-0.66667) wipes in the correct reversed
     vector along the wall.
   - Post-wipe retractions (if any) remain after ; WIPE_END.
   - Unretract commands (G1 E0.8 F1800) execute cleanly after the direct travel.
2. Direct Travel Without Avoidance Detours:
   - Eliminates multi-segment diagonal avoidance detours when reversing wall direction,
     routing directly across between open perimeters with proper Z-hop lift and lower.
3. Perfect Preamble & Layer Transition Integrity:
   - Preserves all layer headers, wipe moves at layer change, M-codes (M625, M624,
     M73, M991, M204), and object labels in their exact original locations.
4. Feature & Surface Immunity:
   - Preserves all features (Top surface, Bottom surface, Infill, Gap fill, Bridges).
   - Any layer containing Top or Bottom surfaces is left 100% untouched.
5. Closed-Loop Protection:
   - Closed loops (start-to-end distance < 2.0 mm) are never reversed or modified.
6. Retraction & Unretract Independence:
   - Operates identically whether retractions are enabled or disabled in the slicer.

Usage:
    python optimize_travel.py <input.gcode> [output.gcode]
"""

import sys
import os
import re
import math
from typing import List, Dict, Tuple, Optional

COORD_RE = re.compile(r'([XYZEF])([-+]?[0-9]*\.?[0-9]+)')

def parse_coords(line: str) -> Dict[str, float]:
    clean = line.split(';')[0].strip()
    return {m.group(1): float(m.group(2)) for m in COORD_RE.finditer(clean)}

def fmt_val(v: float) -> str:
    s = f"{v:.4f}".rstrip('0').rstrip('.')
    return s

def fmt_e(v: float) -> str:
    # Preserve compact OrcaSlicer E formatting (e.g., E.05693 or E-.66667)
    s = f"{v:.5f}".rstrip('0')
    if s.endswith('.'):
        s = s[:-1]
    return s

class ExtrusionBlock:
    def __init__(self):
        self.travel_lines: List[str] = []
        self.p_start: Tuple[float, float] = (0.0, 0.0)
        self.p_end: Tuple[float, float] = (0.0, 0.0)
        self.z_hop: Optional[float] = None
        self.z_layer: Optional[float] = None
        self.travel_f: float = 42000.0
        self.unretract_line: Optional[str] = None
        self.prefix_lines: List[str] = []
        self.points: List[Tuple[float, float]] = []
        self.ext_cmds: List[dict] = []
        self.pre_wipe_lines: List[str] = []
        self.wipe_lines: List[str] = []
        self.post_wipe_lines: List[str] = []

def parse_layer_blocks(slice_lines: List[str], cur_pos: Tuple[float, float]):
    # Find all extrusion lines (G1/G0/G2/G3 with E > 0 and X/Y)
    ext_indices = []
    for i, l in enumerate(slice_lines):
        c = parse_coords(l)
        clean = l.split(';')[0].strip()
        cmd = clean[:2]
        if cmd in ('G1', 'G0', 'G2', 'G3') and 'E' in c and c['E'] > 0 and ('X' in c or 'Y' in c):
            ext_indices.append(i)

    if not ext_indices:
        return slice_lines, [], []

    # Find the travel move before first extrusion outside wipe
    first_travel_idx = -1
    for i in range(ext_indices[0] - 1, -1, -1):
        l = slice_lines[i]
        clean = l.split(';')[0].strip()
        c = parse_coords(l)
        w_start = sum(1 for sl in slice_lines[:i+1] if '; WIPE_START' in sl)
        w_end = sum(1 for sl in slice_lines[:i+1] if '; WIPE_END' in sl)
        cmd = clean[:2]
        if w_start == w_end and cmd in ('G1', 'G0') and ('X' in c or 'Y' in c) and ('E' not in c or c['E'] <= 0):
            first_travel_idx = i
            break

    if first_travel_idx == -1:
        return slice_lines, [], []

    preamble = slice_lines[:first_travel_idx]

    # Group extrusion indices into continuous groups
    groups = []
    cur_g = [ext_indices[0]]
    for idx in ext_indices[1:]:
        has_travel = False
        in_wipe = False
        for k in range(cur_g[-1] + 1, idx):
            l = slice_lines[k]
            clean = l.split(';')[0].strip()
            c = parse_coords(l)
            if '; WIPE_START' in l:
                in_wipe = True
            elif '; WIPE_END' in l:
                in_wipe = False
            cmd = clean[:2]
            if not in_wipe and cmd in ('G1', 'G0') and ('X' in c or 'Y' in c) and ('E' not in c or c['E'] <= 0):
                has_travel = True
                break
        if has_travel:
            groups.append(cur_g)
            cur_g = [idx]
        else:
            cur_g.append(idx)
    groups.append(cur_g)

    blocks: List[ExtrusionBlock] = []

    for g_i, g in enumerate(groups):
        b = ExtrusionBlock()

        if g_i == 0:
            lead_start = first_travel_idx
        else:
            lead_start = groups[g_i - 1][-1] + 1
            for k in range(lead_start, g[0]):
                l = slice_lines[k]
                clean = l.split(';')[0].strip()
                c = parse_coords(l)
                cmd = clean[:2]
                if cmd in ('G1', 'G0') and ('X' in c or 'Y' in c) and ('E' not in c or c['E'] <= 0):
                    w_s = sum(1 for sl in slice_lines[lead_start:k+1] if '; WIPE_START' in sl)
                    w_e = sum(1 for sl in slice_lines[lead_start:k+1] if '; WIPE_END' in sl)
                    if w_s == w_e:
                        lead_start = k
                        break

        last_t_xy = cur_pos
        for k in range(lead_start, g[0]):
            l = slice_lines[k]
            clean = l.split(';')[0].strip()
            c = parse_coords(l)
            cmd = clean[:2]
            if cmd in ('G1', 'G0') and ('X' in c or 'Y' in c) and ('E' not in c or c['E'] <= 0):
                last_t_xy = (c.get('X', last_t_xy[0]), c.get('Y', last_t_xy[1]))
                if 'Z' in c:
                    if b.z_hop is None:
                        b.z_hop = c['Z']
                    else:
                        b.z_layer = c['Z']
                if 'F' in c:
                    b.travel_f = c['F']
                b.travel_lines.append(l)
            elif cmd in ('G1', 'G0') and 'Z' in c and 'X' not in c and 'Y' not in c:
                b.z_layer = c['Z']
                b.travel_lines.append(l)
            elif cmd in ('G1', 'G0') and 'E' in c and c['E'] > 0 and 'X' not in c and 'Y' not in c:
                b.unretract_line = l
                b.travel_lines.append(l)
            else:
                b.prefix_lines.append(l)

        b.p_start = last_t_xy
        b.points = [b.p_start]
        cur_pt = b.p_start

        for k in range(g[0], g[-1] + 1):
            l = slice_lines[k]
            clean = l.split(';')[0].strip()
            c = parse_coords(l)
            cmd = clean[:2]
            if cmd in ('G1', 'G0', 'G2', 'G3') and 'E' in c and c['E'] > 0 and ('X' in c or 'Y' in c):
                cur_pt = (c.get('X', cur_pt[0]), c.get('Y', cur_pt[1]))
                b.points.append(cur_pt)
                b.ext_cmds.append({'dest': cur_pt, 'E': c['E'], 'F': c.get('F'), 'line': l, 'extra': []})
            else:
                if b.ext_cmds:
                    b.ext_cmds[-1]['extra'].append(l)
                else:
                    b.prefix_lines.append(l)

        b.p_end = cur_pt

        if g_i + 1 < len(groups):
            next_travel = g[-1] + 1
            for k in range(g[-1] + 1, len(slice_lines)):
                l = slice_lines[k]
                clean = l.split(';')[0].strip()
                c = parse_coords(l)
                cmd = clean[:2]
                if cmd in ('G1', 'G0') and ('X' in c or 'Y' in c) and ('E' not in c or c['E'] <= 0):
                    w_s = sum(1 for sl in slice_lines[g[-1]+1:k+1] if '; WIPE_START' in sl)
                    w_e = sum(1 for sl in slice_lines[g[-1]+1:k+1] if '; WIPE_END' in sl)
                    if w_s == w_e:
                        next_travel = k
                        break
            trail_end = next_travel
        else:
            trail_end = len(slice_lines)

        # Process lines after g[-1] up to trail_end
        e_in_wipe = False
        w_started = False

        for k in range(g[-1] + 1, trail_end):
            l = slice_lines[k]
            if not l.strip():
                continue

            if '; WIPE_START' in l:
                e_in_wipe = True
                w_started = True
                b.wipe_lines.append(l)
                continue
            if e_in_wipe:
                b.wipe_lines.append(l)
                if '; WIPE_END' in l:
                    e_in_wipe = False
                continue

            if not w_started:
                # Pre-wipe lines: stop object comments, M204, G1 E<0 (retraction before wipe)
                b.pre_wipe_lines.append(l)
            else:
                # Post-wipe lines
                b.post_wipe_lines.append(l)

        blocks.append(b)

    # Separate block-specific lines from layer tail (G17, G3, M625, date)
    tail = []
    if blocks:
        real_post = []
        for l in blocks[-1].post_wipe_lines:
            if not l.strip():
                continue
            clean = l.split(';')[0].strip()
            if clean.startswith('G17') or clean.startswith('G3') or clean.startswith('G2') or 'M625' in l or 'date:' in l:
                tail.append(l)
            else:
                real_post.append(l)
        blocks[-1].post_wipe_lines = real_post

    return preamble, blocks, tail

START_MARKER_REGEX = re.compile(r'^\s*;\s*MACHINE_START_GCODE_END\b')
END_MARKER_REGEX = re.compile(r'^\s*;\s*MACHINE_END_GCODE_START\b')
SPECIAL_PATTERNS_REGEX = re.compile(
    r'(;\s*SKIPPABLE_|;\s*SKIPTYPE:|;\s*FLUSH_|;\s*toolchange|;\s*change_filament|^\s*T\d+\b|^\s*M62[0189]\b|^\s*M400\s+U1\b|^\s*M25\b)'
)

def optimize_gcode(lines: List[str]) -> Tuple[List[str], int, float]:
    # 1. Protect Machine Start G-code
    start_idx = 0
    for i, l in enumerate(lines):
        if START_MARKER_REGEX.match(l):
            start_idx = i + 1
            break
    else:
        for i, l in enumerate(lines):
            if re.match(r'^\s*;\s*(layer num/total_layer_count: 1/|start printing object)', l):
                start_idx = i
                break

    # 2. Protect Machine End G-code
    end_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if END_MARKER_REGEX.match(lines[i]):
            end_idx = i
            break

    header = lines[:start_idx]
    body = lines[start_idx:end_idx]
    footer = lines[end_idx:]

    non_wall_features = {
        'Top surface', 'Bottom surface', 'Internal solid infill',
        'Sparse infill', 'Solid infill', 'Bridge infill', 'Gap infill',
        'Wipe tower', 'Custom'
    }

    # Partition printable body lines into slices respecting layers and object boundaries
    slice_indices = [0]
    for i, l in enumerate(body):
        if i == 0:
            continue
        if re.match(r'^\s*;\s*(CHANGE_LAYER|layer num/total_layer_count: 1/|start printing object|stop printing object)', l):
            slice_indices.append(i)
    slice_indices.append(len(body))

    out_body = []
    cur_pos = (0.0, 0.0)
    for l in header:
        c = parse_coords(l)
        if 'X' in c and 'Y' in c:
            cur_pos = (c['X'], c['Y'])

    total_reversed = 0
    total_saved = 0.0

    for s_idx in range(len(slice_indices) - 1):
        slice_lines = body[slice_indices[s_idx]:slice_indices[s_idx+1]]
        slice_text = ''.join(slice_lines)

        # Slices with no extrusion moves are left 100% untouched
        has_ext = any((l[:2] in ('G1', 'G2', 'G3')) and re.search(r'E\d', l) for l in slice_lines)
        if not has_ext:
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Rule 1: Special gcodes (timelapse, toolchange, flush, manual pause) are left 100% untouched
        if SPECIAL_PATTERNS_REGEX.search(slice_text):
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Rule 2: Slices containing non-wall features (surfaces, infill, wipe tower, custom) are left 100% untouched
        feats = set(re.findall(r'; FEATURE:\s*([^\r\n]+)', slice_text))
        if any(f.strip() in non_wall_features for f in feats):
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        preamble, blocks, tail = parse_layer_blocks(slice_lines, cur_pos)
        if not blocks or len(blocks) < 2:
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Rule 3: Closed loops check. If ANY block forms a closed loop (< 2.0 mm),
        # this slice is part of a closed perimeter / loop model, NOT an open wall model.
        has_closed = any(math.hypot(b.p_end[0] - b.p_start[0], b.p_end[1] - b.p_start[1]) < 2.0 for b in blocks)
        if has_closed:
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Rule 4: All blocks must be open walls (> 10.0 mm span)
        all_open = all(math.hypot(b.p_end[0] - b.p_start[0], b.p_end[1] - b.p_start[1]) > 10.0 for b in blocks)
        if not all_open:
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Rule 5: Simulation for travel savings.
        # Only reverse when reversing eliminates a significant travel detour (> 10.0 mm).
        sim_pos = cur_pos
        rev_flags = []
        saved = 0.0
        for b in blocks:
            d_fwd = math.hypot(sim_pos[0] - b.p_start[0], sim_pos[1] - b.p_start[1])
            d_rev = math.hypot(sim_pos[0] - b.p_end[0], sim_pos[1] - b.p_end[1])
            if d_rev < d_fwd and (d_fwd - d_rev) > 10.0:
                rev_flags.append(True)
                saved += (d_fwd - d_rev)
                sim_pos = b.p_start
            else:
                rev_flags.append(False)
                sim_pos = b.p_end

        if not any(rev_flags) or saved <= 10.0:
            out_body.extend(slice_lines)
            for l in slice_lines:
                c = parse_coords(l)
                if 'X' in c and 'Y' in c:
                    cur_pos = (c['X'], c['Y'])
            continue

        # Perform reversal for qualifying open walls
        total_saved += saved
        out_body.extend(preamble)
        for l in preamble:
            c = parse_coords(l)
            if 'X' in c and 'Y' in c:
                cur_pos = (c['X'], c['Y'])

        for b_idx, b in enumerate(blocks):
            if rev_flags[b_idx]:
                total_reversed += 1

                # 1. Direct travel line to b.p_end (no detour waypoints)
                z_hop_str = f' Z{b.z_hop:g}' if b.z_hop is not None else ''
                out_body.append(f'G1 X{fmt_val(b.p_end[0])} Y{fmt_val(b.p_end[1])}{z_hop_str} F{b.travel_f:g}\n')
                if b.z_layer is not None:
                    out_body.append(f'G1 Z{b.z_layer:g}\n')
                if b.unretract_line:
                    out_body.append(b.unretract_line)

                # 2. Prefix lines (feedrate, acceleration, feature)
                out_body.extend(b.prefix_lines)

                # 3. Extrusions in reverse order
                cooling_extras = []
                for cmd in b.ext_cmds:
                    for el in cmd['extra']:
                        if 'M106' in el:
                            cooling_extras.append(el)

                for s_idx in range(len(b.ext_cmds) - 1, -1, -1):
                    cmd = b.ext_cmds[s_idx]
                    dest = b.points[s_idx]
                    f_val = cmd['F']
                    f_cmd = f' F{f_val:g}' if f_val else ''

                    if s_idx == 0 and cooling_extras:
                        out_body.extend(cooling_extras)
                    else:
                        for el in cmd['extra']:
                            if 'M106' not in el:
                                out_body.append(el)

                    out_body.append(f'G1 X{fmt_val(dest[0])} Y{fmt_val(dest[1])} E{fmt_e(cmd["E"])}{f_cmd}\n')

                # 4. Pre-wipe lines (retraction before wipe G1 E<0, stop object, M73, M204)
                out_body.extend(b.pre_wipe_lines)

                # 5. Wipe in reverse (along wall vector from p_start back toward p_end)
                if b.wipe_lines:
                    out_body.append('; WIPE_START\n')
                    wipe_f = 5400
                    wipe_e = None
                    wipe_dist = 2.0
                    for wl in b.wipe_lines:
                        wc = parse_coords(wl)
                        if 'F' in wc:
                            wipe_f = wc['F']
                        if 'E' in wc:
                            wipe_e = wc['E']
                        if 'X' in wc or 'Y' in wc:
                            orig_wx = wc.get('X', b.p_end[0])
                            orig_wy = wc.get('Y', b.p_end[1])
                            wipe_dist = math.hypot(orig_wx - b.p_end[0], orig_wy - b.p_end[1])
                            if wipe_dist < 0.01:
                                wipe_dist = 2.0

                    out_body.append(f'G1 F{wipe_f:g}\n')

                    dx = b.p_end[0] - b.p_start[0]
                    dy = b.p_end[1] - b.p_start[1]
                    L = math.hypot(dx, dy)
                    w_dist = min(wipe_dist, L)
                    wx = b.p_start[0] + (dx / L) * w_dist
                    wy = b.p_start[1] + (dy / L) * w_dist
                    e_str = f' E{fmt_e(wipe_e)}' if wipe_e is not None else ''
                    out_body.append(f'G1 X{fmt_val(wx)} Y{fmt_val(wy)}{e_str}\n')
                    out_body.append('; WIPE_END\n')
                    cur_pos = (wx, wy)
                else:
                    cur_pos = b.p_start

                # 6. Post-wipe lines
                out_body.extend(b.post_wipe_lines)
            else:
                # Keep block forward in exact original order
                out_body.extend(b.travel_lines)
                out_body.extend(b.prefix_lines)
                for cmd in b.ext_cmds:
                    out_body.append(cmd['line'])
                    out_body.extend(cmd['extra'])
                out_body.extend(b.pre_wipe_lines)
                out_body.extend(b.wipe_lines)
                out_body.extend(b.post_wipe_lines)

                if b.wipe_lines:
                    for wl in b.wipe_lines:
                        wc = parse_coords(wl)
                        if 'X' in wc and 'Y' in wc:
                            cur_pos = (wc['X'], wc['Y'])
                else:
                    cur_pos = b.p_end

        out_body.extend(tail)

    out_lines = header + out_body + footer
    return out_lines, total_reversed, total_saved

def main():
    if len(sys.argv) < 2:
        print("Usage: python optimize_travel.py <input.gcode> [output.gcode]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else input_file

    if not os.path.exists(input_file):
        print(f"Error: file '{input_file}' not found.")
        sys.exit(1)

    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines, count, saved = optimize_gcode(lines)

    temp_output = output_file + '.tmp' if output_file == input_file else output_file
    with open(temp_output, 'w', encoding='utf-8', newline='') as f:
        f.writelines(new_lines)

    if output_file == input_file:
        os.replace(temp_output, output_file)

    print(f"[optimize_travel] Successfully optimized {input_file}: {count} open walls reversed, {saved:.1f} mm travel saved.")

if __name__ == '__main__':
    main()
