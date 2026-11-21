"""Map model for the Ghost bot: obstacle + line-of-sight rasters, Dijkstra, concealment score.

Standard library only. Everything here is built from the static map in hello.map.dir
(v2/world.json + v2/terrain.i16, protocol.md section 11).

Grids
- fine grid  (FINE = 2 m): `solid` (blocks walking) and `tall` (blocks sight), rasterized from the
  building / stadium prisms. Bridges are ignored (walk-under or walkable deck; see research.md).
- move grid  (CS = 4 m): `block` = fine solids (>= 2 of 4 sub-cells) or terrain steeper than 50 deg;
  `pen` = small extra cost next to blocked cells so paths keep some clearance.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys
from array import array

CS = 4.0          # move-grid cell size (m)
FINE = 2.0        # fine (sight / solid) grid cell size (m)
SLOPE_CM = 119    # tan(50 deg) = 1.19 -> 119 cm rise per 1 m run
EYE_BLOCK = 8.0   # a prism taller than this above ground blocks sight at eye height
STEP_UP = 1.2     # taller than this (above ground) = wall
CLEARANCE = 9.0   # prism base this far above ground can be walked under

_NEI = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
        (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]


class Terrain:
    def __init__(self, path: str, nx: int, ny: int, minx: float, miny: float, step: float):
        a = array("h")
        with open(path, "rb") as f:
            a.frombytes(f.read())
        if sys.byteorder == "big":
            a.byteswap()
        if len(a) < nx * ny:
            raise ValueError("terrain file too small")
        self.a, self.nx, self.ny, self.minx, self.miny, self.step = a, nx, ny, minx, miny, step

    def h(self, x: float, y: float) -> float:
        """Ground height (m), bilinear."""
        fx = (x - self.minx) / self.step
        fy = (y - self.miny) / self.step
        mx = self.nx - 1.001
        my = self.ny - 1.001
        fx = 0.0 if fx < 0 else (mx if fx > mx else fx)
        fy = 0.0 if fy < 0 else (my if fy > my else fy)
        i = int(fx)
        j = int(fy)
        ax = fx - i
        ay = fy - j
        k = j * self.nx + i
        a = self.a
        n = self.nx
        return 0.01 * (a[k] * (1 - ax) * (1 - ay) + a[k + 1] * ax * (1 - ay)
                       + a[k + n] * (1 - ax) * ay + a[k + n + 1] * ax * ay)


class NavMap:
    def __init__(self, mapdir: str):
        with open(os.path.join(mapdir, "v2", "world.json")) as f:
            wj = json.load(f)
        t = wj["terrain"]
        self.terrain = Terrain(os.path.join(mapdir, "v2", "terrain.i16"),
                               t["nx"], t["ny"], t["minX"], t["minY"], t["step"])
        self.ox = float(t["minX"])
        self.oy = float(t["minY"])
        self.width = t["nx"] * t["step"]
        self.height = t["ny"] * t["step"]
        # fine grid
        self.fnx = int(math.ceil(self.width / FINE))
        self.fny = int(math.ceil(self.height / FINE))
        self.solid = bytearray(self.fnx * self.fny)
        self.tall = bytearray(self.fnx * self.fny)
        self._rasterize(wj.get("prisms", []))
        # move grid
        self.nx = int(math.ceil(self.width / CS))
        self.ny = int(math.ceil(self.height / CS))
        self.block = bytearray(self.nx * self.ny)
        self.pen = [0.0] * (self.nx * self.ny)
        self.learned = set()          # cells blocked after getting stuck
        self._build_move_grid()
        self._expo_cache = {}

    # ------------------------------------------------------------------ rasterization
    def gh(self, x: float, y: float) -> float:
        return self.terrain.h(x, y)

    def _rasterize(self, prisms) -> None:
        ox, oy, fnx, fny = self.ox, self.oy, self.fnx, self.fny
        terrain = self.terrain
        for p in prisms:
            kind = p.get("kind")
            if kind == "bridge":
                continue
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            rings = [ring] + [h for h in (p.get("holes") or []) if len(h) >= 6]
            edges = []
            for r in rings:
                n = len(r) // 2
                for k in range(n):
                    k2 = (k + 1) % n
                    edges.append((r[2 * k], r[2 * k + 1], r[2 * k2], r[2 * k2 + 1]))
            ys = ring[1::2]
            j0 = max(0, int((min(ys) - oy) / FINE))
            j1 = min(fny - 1, int((max(ys) - oy) / FINE))
            base = p.get("base", 0.0)
            top = p.get("top", 0.0)
            for j in range(j0, j1 + 1):
                y = oy + (j + 0.5) * FINE
                xs = []
                for (x0, y0, x1, y1) in edges:
                    if (y0 <= y < y1) or (y1 <= y < y0):
                        xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
                if len(xs) < 2:
                    continue
                xs.sort()
                for k in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil((xs[k] - ox) / FINE - 0.5)))
                    i1 = min(fnx - 1, int(math.floor((xs[k + 1] - ox) / FINE - 0.5)))
                    for i in range(i0, i1 + 1):
                        g = terrain.h(ox + (i + 0.5) * FINE, y)
                        h = top - g
                        if h > STEP_UP and base - g < CLEARANCE:
                            idx = j * fnx + i
                            self.solid[idx] = 1
                            if h >= EYE_BLOCK:
                                self.tall[idx] = 1

    def _build_move_grid(self) -> None:
        nx, ny, fnx, fny = self.nx, self.ny, self.fnx, self.fny
        solid, block = self.solid, self.block
        arr = self.terrain.a
        tnx, tny = self.terrain.nx, self.terrain.ny
        for j in range(ny):
            for i in range(nx):
                c = 0
                for dj in (0, 1):
                    fj = 2 * j + dj
                    if fj >= fny:
                        continue
                    for di in (0, 1):
                        fi = 2 * i + di
                        if fi < fnx and solid[fj * fnx + fi]:
                            c += 1
                bad = c >= 2
                if not bad:
                    a0 = int(CS) * i
                    b0 = int(CS) * j
                    for b in (b0, b0 + 2, b0 + 4):
                        if b + 1 >= tny:
                            continue
                        row = b * tnx
                        for a in (a0, a0 + 2, a0 + 4):
                            if a + 1 >= tnx:
                                continue
                            v = arr[row + a]
                            if abs(arr[row + a + 1] - v) > SLOPE_CM or abs(arr[row + tnx + a] - v) > SLOPE_CM:
                                bad = True
                                break
                        if bad:
                            break
                if bad:
                    block[j * nx + i] = 1
        pen = self.pen
        for j in range(ny):
            for i in range(nx):
                k = j * nx + i
                if block[k]:
                    continue
                near = False
                for di, dj, _ in _NEI:
                    a, b = i + di, j + dj
                    if 0 <= a < nx and 0 <= b < ny and block[b * nx + a]:
                        near = True
                        break
                if near:
                    pen[k] = 1.2

    # ------------------------------------------------------------------ cells
    def cell_of(self, x: float, y: float):
        i = int((x - self.ox) / CS)
        j = int((y - self.oy) / CS)
        i = 0 if i < 0 else (self.nx - 1 if i >= self.nx else i)
        j = 0 if j < 0 else (self.ny - 1 if j >= self.ny else j)
        return i, j

    def center_of(self, i: int, j: int):
        return self.ox + (i + 0.5) * CS, self.oy + (j + 0.5) * CS

    def is_blocked(self, i: int, j: int) -> bool:
        k = j * self.nx + i
        return bool(self.block[k]) or k in self.learned

    def nearest_free(self, i: int, j: int, maxr: int = 10):
        if 0 <= i < self.nx and 0 <= j < self.ny and not self.is_blocked(i, j):
            return i, j
        for r in range(1, maxr + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    a, b = i + di, j + dj
                    if 0 <= a < self.nx and 0 <= b < self.ny and not self.is_blocked(a, b):
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, a, b)
            if best:
                return best[1], best[2]
        return None

    def block_at(self, x: float, y: float) -> bool:
        i, j = self.cell_of(x, y)
        return self.is_blocked(i, j)

    def learn_block(self, x: float, y: float) -> None:
        i, j = self.cell_of(x, y)
        self.learned.add(j * self.nx + i)

    # ------------------------------------------------------------------ queries
    def free_ray(self, x: float, y: float, dx: float, dy: float, length: float) -> bool:
        """True if the straight segment from (x,y) along unit (dx,dy) stays on free move cells."""
        s = 2.0
        while s <= length + 1e-6:
            if self.block_at(x + dx * s, y + dy * s):
                return False
            s += 2.0
        return True

    def los(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """Line of sight over tall buildings (2-D). True = clear."""
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        n = int(d / 1.5) + 1
        sx, sy = dx / n, dy / n
        tall, fnx, fny, ox, oy = self.tall, self.fnx, self.fny, self.ox, self.oy
        x, y = x0, y0
        for _ in range(n):
            x += sx
            y += sy
            i = int((x - ox) / FINE)
            j = int((y - oy) / FINE)
            if 0 <= i < fnx and 0 <= j < fny and tall[j * fnx + i]:
                return False
        return True

    def ray_len(self, x: float, y: float, ang: float, maxlen: float) -> float:
        dx, dy = math.cos(ang), math.sin(ang)
        tall, fnx, fny, ox, oy = self.tall, self.fnx, self.fny, self.ox, self.oy
        s = FINE
        while s <= maxlen:
            i = int((x + dx * s - ox) / FINE)
            j = int((y + dy * s - oy) / FINE)
            if not (0 <= i < fnx and 0 <= j < fny):
                return s
            if tall[j * fnx + i]:
                return s
            s += FINE
        return maxlen

    def exposure(self, x: float, y: float, rays: int = 16, maxlen: float = 80.0):
        """(mean open distance over `rays` directions, angle of the most open direction).
        Low mean = concealed. Cached per fine cell."""
        key = (int((x - self.ox) / FINE), int((y - self.oy) / FINE))
        v = self._expo_cache.get(key)
        if v is not None:
            return v
        tot = 0.0
        best = (-1.0, 0.0)
        for k in range(rays):
            a = 2 * math.pi * k / rays
            L = self.ray_len(x, y, a, maxlen)
            tot += L
            if L > best[0]:
                best = (L, a)
        v = (tot / rays, best[1])
        self._expo_cache[key] = v
        return v

    # ------------------------------------------------------------------ planning
    def disc_components(self, cx: float, cy: float, r: float):
        """Connected components (8-neighbour, same corner rule as dijkstra) of the walkable cells inside a disc.
        Returns ({cell index: component id}, [component sizes])."""
        nx, ny = self.nx, self.ny
        i0, j0 = self.cell_of(cx - r, cy - r)
        i1, j1 = self.cell_of(cx + r, cy + r)
        r2 = r * r
        block, learned = self.block, self.learned
        ox, oy = self.ox + CS * 0.5, self.oy + CS * 0.5
        label, sizes = {}, []

        def ok(i, j):
            if i < i0 or j < j0 or i > i1 or j > j1:
                return False
            k = j * nx + i
            if block[k] or (learned and k in learned):
                return False
            x = ox + i * CS - cx
            y = oy + j * CS - cy
            return x * x + y * y <= r2

        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                k = j * nx + i
                if k in label or not ok(i, j):
                    continue
                cid = len(sizes)
                sizes.append(0)
                label[k] = cid
                stack = [(i, j)]
                while stack:
                    a, b = stack.pop()
                    sizes[cid] += 1
                    for di, dj, _c in _NEI:
                        a2, b2 = a + di, b + dj
                        if not ok(a2, b2):
                            continue
                        k2 = b2 * nx + a2
                        if k2 in label:
                            continue
                        if di and dj and (block[b * nx + a2] or block[b2 * nx + a]):
                            continue
                        label[k2] = cid
                        stack.append((a2, b2))
        return label, sizes

    def zone_pen(self, zx: float, zy: float, zr: float, margin: float = 4.0, base: float = 3.0, slope: float = 2.5):
        """Per-cell extra path cost for cells outside the (soft) safe circle: base + slope per cell outside."""
        nx, ny = self.nx, self.ny
        lim = max(1.0, zr - margin)
        out = [0.0] * (nx * ny)
        sqrt = math.sqrt
        hx = self.ox + CS * 0.5 - zx
        for j in range(ny):
            y = self.oy + (j + 0.5) * CS - zy
            y2 = y * y
            row = j * nx
            if y2 > (lim + 300.0) ** 2:      # far outside: constant large penalty (cheap path)
                far = base + slope * (abs(y) - lim) / CS
                for i in range(nx):
                    out[row + i] = far
                continue
            for i in range(nx):
                x = hx + i * CS
                d = sqrt(x * x + y2)
                if d > lim:
                    out[row + i] = base + slope * (d - lim) / CS
        return out

    def dijkstra(self, si: int, sj: int, zp=None):
        nx, ny = self.nx, self.ny
        INF = 1e18
        dist = [INF] * (nx * ny)
        par = [-1] * (nx * ny)
        block, pen, learned = self.block, self.pen, self.learned
        s = sj * nx + si
        dist[s] = 0.0
        h = [(0.0, s)]
        pop, push = heapq.heappop, heapq.heappush
        nei = _NEI
        while h:
            d, u = pop(h)
            if d > dist[u]:
                continue
            ui = u % nx
            uj = u // nx
            for di, dj, c in nei:
                vi = ui + di
                vj = uj + dj
                if vi < 0 or vj < 0 or vi >= nx or vj >= ny:
                    continue
                v = vj * nx + vi
                if block[v] or (learned and v in learned):
                    continue
                if di and dj:
                    a1 = uj * nx + vi
                    a2 = vj * nx + ui
                    if block[a1] or block[a2]:
                        continue
                nd = d + c + pen[v]
                if zp is not None:
                    z = zp[v]
                    if z >= 1e17:          # hard-forbidden (outside the zone)
                        continue
                    nd += z
                if nd < dist[v]:
                    dist[v] = nd
                    par[v] = u
                    push(h, (nd, v))
        return dist, par

    def path_cells(self, par, goal_k: int):
        out = []
        k = goal_k
        guard = self.nx * self.ny
        while k != -1 and guard > 0:
            out.append(k)
            k = par[k]
            guard -= 1
        out.reverse()
        return out

    def seg_free(self, x0, y0, x1, y1, side: float = 2.0) -> bool:
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        ux, uy = dx / d, dy / d
        px, py = -uy, ux
        s = 0.0
        while s <= d:
            cx, cy = x0 + ux * s, y0 + uy * s
            if self.block_at(cx, cy):
                return False
            if side > 0 and (self.block_at(cx + px * side, cy + py * side) or self.block_at(cx - px * side, cy - py * side)):
                return False
            s += 2.0
        return True

    def smooth(self, start, pts):
        """String-pull a list of (x,y) waypoints starting from `start`. Returns polyline incl. start."""
        out = [start]
        cur = start
        i = 0
        n = len(pts)
        while i < n:
            j = n - 1
            while j > i and not self.seg_free(cur[0], cur[1], pts[j][0], pts[j][1]):
                j -= 1
            out.append(pts[j])
            cur = pts[j]
            i = j + 1
        return out
