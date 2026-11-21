#!/usr/bin/env python3
"""Write a synthetic map in the exact v2/world.json + v2/terrain.i16 format (dev/test only).

    python3 make_synth_map.py OUTDIR [seed]

Same bounds/terrain grid as the real campus map: 1070 x 724 m at 1 m, origin (-513,-334).
Contains buildings (several with courtyard holes), a stadium-like ring, a long wall that forms a
dead-end pocket (to exercise path planning), one steep cliff band, and a couple of bridges.
"""
import json
import math
import os
import random
import struct
import sys

NX, NY, MINX, MINY = 1070, 724, -513, -334


def rect(x0, y0, x1, y1):
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def main(out, seed=7):
    rng = random.Random(seed)
    os.makedirs(os.path.join(out, "v2"), exist_ok=True)
    vals = []
    for j in range(NY):
        y = MINY + j
        for i in range(NX):
            x = MINX + i
            h = 2.0 * math.sin(x / 80.0) + 1.5 * math.cos(y / 60.0) + 8.0 * math.exp(-((x - 300) ** 2 + (y - 200) ** 2) / 4000.0)
            if 100 <= x <= 102 and -200 <= y <= 100:      # steep cliff band (3 m step over 2 m)
                h += 3.0 if x == 102 else 1.5
            vals.append(int(round(h * 100)))
    with open(os.path.join(out, "v2", "terrain.i16"), "wb") as f:
        f.write(struct.pack("<%dh" % len(vals), *vals))
    prisms = []

    def ground(x, y):
        i = min(NX - 1, max(0, int(x - MINX)))
        j = min(NY - 1, max(0, int(y - MINY)))
        return vals[j * NX + i] / 100.0

    def add(kind, ring, height, holes=None, base_off=-1.0):
        cx = sum(ring[0::2]) / (len(ring) / 2)
        cy = sum(ring[1::2]) / (len(ring) / 2)
        g = ground(cx, cy)
        prisms.append({"kind": kind, "block": -1, "base": g + base_off, "top": g + height, "ring": ring, "holes": holes or []})

    for _ in range(140):
        w, h = rng.uniform(14, 48), rng.uniform(14, 48)
        x = rng.uniform(MINX + 40, MINX + NX - 80)
        y = rng.uniform(MINY + 40, MINY + NY - 80)
        hgt = rng.choice([4, 6, 10, 14, 22, 35])
        holes = None
        if w > 30 and h > 30 and rng.random() < 0.4:
            holes = [rect(x + 8, y + 8, x + w - 8, y + h - 8)]
        add("building", rect(x, y, x + w, y + h), hgt, holes)
    # a stadium-like ring around (-120, -60) with a hole (field) and one gap-free wall
    cx, cy = -120.0, -60.0
    ring = []
    hole = []
    for a in range(24):
        ang = a / 24 * 2 * math.pi
        ring += [cx + 95 * math.cos(ang), cy + 75 * math.sin(ang)]
        hole += [cx + 65 * math.cos(ang), cy + 45 * math.sin(ang)]
    add("stadium", ring, 28, [hole])
    # dead-end pocket: U shaped wall opening south
    add("building", rect(200, -180, 280, -170), 15)
    add("building", rect(200, -180, 210, -110), 15)
    add("building", rect(270, -180, 280, -110), 15)
    add("bridge", rect(-60, 150, -20, 158), 3, base_off=1.0)
    add("bridge", rect(250, 60, 262, 100), 3, base_off=10.0)
    world = {
        "version": 2, "origin": {"lat": 40.0005, "lon": -83.0165}, "frame": "enu",
        "bounds": {"minX": -512.38, "maxX": 555.08, "minY": -333.09, "maxY": 388.64},
        "rules": {"maxStep": 1.5, "maxSlopeDeg": 50, "canopyDensity": 0.35, "canopyBlockT": 0.25},
        "terrain": {"file": "terrain.i16", "format": "int16 little-endian, centimeters, row-major (row j = y), vertex grid",
                    "minX": MINX, "minY": MINY, "step": 1, "nx": NX, "ny": NY, "scale": 0.01},
        "prisms": prisms, "blocks": [], "stadium": {},
        "trees": {"fields": ["x", "y", "z", "height", "crownR", "crownBase", "trunkR", "osm", "crownX", "crownY"], "data": []},
        "cars": [], "parking": [], "water": [], "barriers": [], "stats": {},
    }
    with open(os.path.join(out, "v2", "world.json"), "w") as f:
        json.dump(world, f)
    print("wrote", out, len(prisms), "prisms")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 7)
