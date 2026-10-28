#!/usr/bin/env python3
"""
Renders a 2D/isometric view of G-code moves (cyan for extrusion, white for travel)
similar to gcode_visual.png.
"""
import re
from PIL import Image, ImageDraw

COORD_RE = re.compile(r'([XYZEF])([-+]?[0-9]*\.?[0-9]+)')

def parse_coords(line):
    res = {}
    clean = line.split(';')[0].strip()
    for m in COORD_RE.finditer(clean):
        res[m.group(1)] = float(m.group(2))
    return res

def parse_moves(filename):
    moves = []
    x, y, z = 0.0, 0.0, 0.0
    with open(filename, 'r') as f:
        for line in f:
            clean = line.split(';')[0].strip()
            if clean.startswith('G1') or clean.startswith('G0'):
                c = parse_coords(clean)
                nx = c.get('X', x)
                ny = c.get('Y', y)
                nz = c.get('Z', z)
                is_extrude = 'E' in c and c['E'] > 0 and (nx != x or ny != y)
                if nx != x or ny != y or nz != z:
                    moves.append(((x, y, z), (nx, ny, nz), is_extrude))
                x, y, z = nx, ny, nz
    return moves

def render(moves, out_png, width=400, height=1000):
    img = Image.new('RGB', (width, height), (40, 40, 40))
    draw = ImageDraw.Draw(img)
    
    # Filter out start moves outside print area if needed
    filtered = [m for m in moves if m[0][0] > 100 and m[1][0] > 100]
    if not filtered:
        filtered = moves
        
    # Project: let's use an oblique 3D/isometric projection similar to gcode_visual.png
    # In gcode_visual.png: Y is vertical, X is horizontal, Z is slightly oblique
    # Let's map X to screen X, Y to screen Y
    xs = [m[0][0] for m in filtered] + [m[1][0] for m in filtered]
    ys = [m[0][1] for m in filtered] + [m[1][1] for m in filtered]
    zs = [m[0][2] for m in filtered] + [m[1][2] for m in filtered]
    
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    
    def project(pt):
        px, py, pz = pt
        # Perspective / oblique
        # Map: X spans ~10mm, Y spans ~50mm
        # We can map with margin
        margin = 50
        scale_x = (width - 2 * margin) / max(1.0, (max_x - min_x + 5.0))
        scale_y = (height - 2 * margin) / max(1.0, (max_y - min_y))
        scale = min(scale_x * 0.7, scale_y)
        
        # Tilt slightly
        sx = margin + (px - min_x) * scale_x * 0.8 + (pz - min_z) * 15.0
        sy = height - margin - (py - min_y) * scale_y + (pz - min_z) * 5.0
        return (sx, sy)

    # Draw travel moves first (white/grey)
    for p1, p2, is_ext in filtered:
        if not is_ext:
            s1 = project(p1)
            s2 = project(p2)
            draw.line([s1, s2], fill=(240, 240, 240), width=1)
            
    # Draw extrusion moves (cyan)
    for p1, p2, is_ext in filtered:
        if is_ext:
            s1 = project(p1)
            s2 = project(p2)
            draw.line([s1, s2], fill=(0, 255, 255), width=2)
            
    img.save(out_png)
    print(f"Saved render to {out_png}")

if __name__ == '__main__':
    orig_moves = parse_moves('plate_1.gcode')
    render(orig_moves, 'visual_orig.png')
    opt_moves = parse_moves('plate_1_optimized.gcode')
    render(opt_moves, 'visual_optimized.png')

