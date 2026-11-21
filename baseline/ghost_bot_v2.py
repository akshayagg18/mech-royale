"""Ghost-v2: a resource-and-survival bot for Builders Mech Royale (protocol mechfight/1).

v2 = v1 (zone-safe pathing, concealed hide spots, stealth walking, dodging) plus: crate / drone / cube / weapon
farming early, a utility-based duel engine (engage when we win the damage race or are forced, hold the weapon-specific
range, strafe, stay inside the zone), a dynamic retreat-and-recover rule, and duel-oriented upgrade picks.
Every behaviour has a switch in CFG so an official submission can change one factor at a time.

v1 notes (still true):

Placement is all that scores, so Ghost tries to be the last one anybody finds:

1. Stay inside the *next* zone circle (a mech inside it never takes zone damage).
2. Sit still at the most concealed reachable spot inside that circle (computed from the real
   building footprints): omni sight is only 45 m and standing still / walking <= 5 m/s is silent.
3. Fight only in self-defence (provoked, adjacent, or a nearly dead target), always torso-facing the
   attacker (rear armor x1.5), dodge laser telegraphs and mortar shells, break contact when hurt so
   the 45 HP/s regen can refill the bar.
4. Opportunistically take a laser/lightning (never a mortar), nearby cubes, and an uncontested
   supply drop; pick survival upgrades first.

Standard library only. Heavy map work (rasterization, Dijkstra, concealment) runs in a worker thread
so the 50 ms decision loop never waits for it; until it is ready the bot walks straight at the next
circle centre.
"""
from __future__ import annotations

import math
import random
import sys
import threading
import time
import traceback
from collections import deque

import mechfight as mf

try:
    from nav import NavMap, CS
except Exception:  # pragma: no cover - the bot still plays (blind) if the map code fails
    NavMap = None
    CS = 4.0
    mf.log(traceback.format_exc())

TAU = 2 * math.pi

CFG = dict(
    FARM=True,           # seek crates / drones / cubes / weapons while calm
    FARM_UNTIL=200.0,    # crates and drones only before this match time (cubes and weapons any time)
    FARM_HP=0.55,        # do not farm below this HP fraction
    FARM_STAND=30.0,     # firing distance for crates and drones
    OPP=False,           # opportunistic engagement of visible enemy mechs when the damage race favours us
    OPP_RANGE=105.0,
    OPP_MAXN=2,          # at most this many enemy mechs within 115 m for an opportunistic fight
    FAVOR=0.9,           # time-to-kill < FAVOR * time-to-die
    CLOSE=55.0,          # any enemy mech this close is fought (it will catch us anyway)
    RETREAT=0.28,        # HP fraction below which we disengage (+0.07 per extra enemy) unless winning
    RECOVER=True,        # break line of sight and regenerate instead of trading to the death
    ZONE_BUF=14.0,       # keep this margin inside the circle that will exist in 4 s while fighting
    RECOVER_RANGE=90.0,  # below the retreat threshold: farther than this we just break contact; closer we shoot while backing off
    CALM_R=100.0,        # farm only when no enemy mech is visible within this range
    DROP_REACH=260.0,    # supply drops: go for any drop we can reach before it lands + a few seconds
    SOUND_AIM=True,      # turn the torso toward heard gun/laser/step sounds when standing still (wider sight wedge)
)
UPGRADE_ORDER = ["gun_damage", "plating", "armor", "gun_rate", "nanites", "laser_damage", "laser_charge",
                 "lightning_efficiency", "gyros", "sensors", "gun_mag", "servos", "gun_velocity", "lightning_range",
                 "lightning_chain", "reactor", "capacitor", "jets", "laser_pierce"]


def zof(pos, default):
    """Height of a position that may or may not carry a z component."""
    return pos[2] if len(pos) > 2 else default


def angdiff(a: float, b: float) -> float:
    return (a - b + math.pi) % TAU - math.pi


def fin(v, default=0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def norm2(x: float, y: float):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-9 else (0.0, 0.0)


class Ghost:
    name = "ghost-v2"

    def __init__(self, hello):
        sys.setswitchinterval(0.001)
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        self.rng = random.Random(hello.get("seed", 0))
        self.mapdir = hello.get("map", {}).get("dir", "/arena")
        mech = self.cfg.get("mech", {})
        self.gravity = self.cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22.0)
        self.step_speed = float(self.cfg.get("sound", {}).get("step_speed", 5.0))   # faster than this makes footsteps
        self.min_pitch = mech.get("min_pitch", -0.7)
        self.max_pitch = mech.get("max_pitch", 1.3)
        self.nav = None
        self.plan = None          # adopted plan
        self.plan_new = None      # handed over by the worker
        self.ver = 0
        self.want = None
        self.pk = 0
        self._req = None
        self._ev = threading.Event()
        self.last_hit_by = {}
        self.last_damage_t = -99.0
        self.last_bearing = None
        self.last_npc_hit = -99.0
        self.hist = deque(maxlen=40)
        self.jitter_until = 0.0
        self.jitter = (0.0, 0.0)
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.loot = None          # {"id", "pos", "kind", "best", "since"}
        self.black = {}           # loot id -> blacklisted until t
        self.last_cmd = (0.0, 0.0)
        self.moving_since = None
        self.keep_old = False
        self.tmpl = None          # request template for the current want
        self._zplan = None        # (cx, cy, r): circle the path should stay inside
        self.req_t = -99.0        # game time of the last plan request
        self._t = 0.0
        self.mode = "boot"
        self.laser_hold = 0.0
        self.snd = None           # (t, bearing, dist band, kind) of the latest relevant sound
        self.fight_id = None
        self.cmode = "none"
        self.recover_until = 0.0
        self.ft = None            # current farm target {"id","best","since","kind","shooting"}
        threading.Thread(target=self._boot, daemon=True).start()

    # ------------------------------------------------------------------ worker thread
    def _boot(self):
        t0 = time.time()
        try:
            if NavMap is not None:
                self.nav = NavMap(self.mapdir)
                mf.log(f"nav ready in {time.time() - t0:.1f}s ({self.nav.nx}x{self.nav.ny} cells)")
        except Exception:
            mf.log("nav load failed:\n" + traceback.format_exc())
            return
        while True:
            self._ev.wait()
            self._ev.clear()
            req = self._req
            if not req:
                continue
            try:
                res = self._compute(req)
            except Exception:
                mf.log("plan failed:\n" + traceback.format_exc())
                res = None
            if res is not None:
                self.plan_new = res

    def _request(self, req, keep=False):
        """Ask the worker for a plan. keep=True: keep following the old plan until the new one lands."""
        self.ver += 1
        req["ver"] = self.ver
        self.req_t = self._t
        self._req = req
        if not keep:
            self.plan = None
        self._ev.set()

    def _issue(self, tmpl, x, y, want, keep=False):
        req = dict(tmpl)
        req["pos"] = (x, y)
        req["want"] = want
        req["zone"] = self._zplan
        self._request(req, keep)

    def _replan(self, t, x, y):
        """Re-plan the current want from where we are (keeps following the old plan meanwhile).
        Skipped while a recent request is still in flight so results are never starved."""
        if self.tmpl is not None and self.nav is not None and t - self.req_t > 1.5:
            self._issue(self.tmpl, x, y, self.want, True)

    def _select_hide(self, dist, ncx, ncy, nr, label, sizes, amax):
        """Pick the hiding cell inside the next circle (low exposure, short trip, biggest connected region)."""
        nav = self.nav
        nx = nav.nx
        INF = 1e17
        rc = max(6.0, min(0.8 * nr, nr - 12.0))
        i0, j0 = nav.cell_of(ncx - rc, ncy - rc)
        i1, j1 = nav.cell_of(ncx + rc, ncy + rc)
        stride = 1
        while ((i1 - i0 + 1) * (j1 - j0 + 1)) / (stride * stride) > 2500:
            stride += 1
        best = None
        for j in range(j0, j1 + 1, stride):
            for i in range(i0, i1 + 1, stride):
                k = j * nx + i
                d = dist[k]
                if d >= INF:
                    continue
                cx, cy = nav.center_of(i, j)
                dc = math.hypot(cx - ncx, cy - ncy)
                if dc > rc:
                    continue
                e, a = nav.exposure(cx, cy)
                cost = e + 0.025 * d * CS + 0.08 * dc
                if label is not None:
                    cid = label.get(k)
                    cost += 100.0 * (1.0 - sizes[cid] / amax) if cid is not None else 100.0
                if best is None or cost < best[0]:
                    best = (cost, k, cx, cy, a, e)
        if best is None:  # nothing reachable inside the disc: closest reachable cell to the centre
            bd = None
            for k in range(nav.nx * nav.ny):
                if dist[k] >= INF:
                    continue
                cx, cy = nav.center_of(k % nx, k // nx)
                dc = math.hypot(cx - ncx, cy - ncy)
                if bd is None or dc < bd:
                    bd = dc
                    best = (dc, k, cx, cy, 0.0, 0.0)
        return best

    def _poly(self, par, k, x, y, hx, hy):
        nav = self.nav
        nx = nav.nx
        cells = nav.path_cells(par, k)
        pts = [nav.center_of(c % nx, c // nx) for c in cells[1:]]
        if not pts:
            pts = [(hx, hy)]
        return nav.smooth((x, y), pts)

    @staticmethod
    def _outside_len(poly, zc):
        """Length of the polyline lying outside the circle zc=(x, y, r), sampled every ~3 m."""
        tot = 0.0
        for (ax, ay), (bx, by) in zip(poly, poly[1:]):
            L = math.hypot(bx - ax, by - ay)
            n = max(1, int(L / 3.0))
            for q in range(n):
                f = (q + 0.5) / n
                if math.hypot(ax + (bx - ax) * f - zc[0], ay + (by - ay) * f - zc[1]) > zc[2]:
                    tot += L / n
        return tot

    def _compute(self, req):
        nav = self.nav
        x, y = req["pos"]
        s = nav.nearest_free(*nav.cell_of(x, y))
        if s is None:
            return None
        zc = req.get("zone")
        zp = nav.zone_pen(zc[0], zc[1], zc[2]) if zc else None
        dist, par = nav.dijkstra(s[0], s[1], zp)
        INF = 1e17
        nx = nav.nx
        if req["kind"] == "point":
            gx, gy = req["goal"]
            g = nav.nearest_free(*nav.cell_of(gx, gy), maxr=4)
            if g is None or dist[g[1] * nx + g[0]] >= INF:
                return None
            k = g[1] * nx + g[0]
            hx, hy = nav.center_of(*g)
            ang = 0.0
            expo = 0.0
            poly = self._poly(par, k, x, y, hx, hy)
        else:
            ncx, ncy = req["nc"]
            nr = req["nr"]
            label, sizes, amax = None, None, 1
            if nr <= 260.0:
                # future circles nest inside the next one: prefer the biggest connected region of it, so any
                # later circle stays reachable without leaving the zone (rivers/cliffs/buildings can split a disc)
                rc = max(6.0, min(0.8 * nr, nr - 12.0))
                label, sizes = nav.disc_components(ncx, ncy, max(nr - 6.0, rc + 2.0))
                amax = max(sizes) if sizes else 1
            best = self._select_hide(dist, ncx, ncy, nr, label, sizes, amax)
            if best is None:
                return None
            _, k, hx, hy, ang, expo = best
            poly = self._poly(par, k, x, y, hx, hy)
            if zc and zp[s[1] * nx + s[0]] <= 0.0 and self._outside_len(poly, zc) > 12.0:
                # the cheapest route leaves the zone for a long stretch (barrier inside the circle): stay inside
                # and wait at the reachable cell closest to the target instead
                zph = [1e18 if v > 0.0 else 0.0 for v in zp]
                dist2, par2 = nav.dijkstra(s[0], s[1], zph)
                b2 = self._select_hide(dist2, ncx, ncy, nr, label, sizes, amax)
                if b2 is not None:
                    _, k, hx, hy, ang, expo = b2
                    poly = self._poly(par2, k, x, y, hx, hy)
        return {"ver": req["ver"], "kind": req["kind"], "H": (hx, hy), "open_dir": ang,
                "expo": expo, "path": poly, "want": req.get("want")}

    # ------------------------------------------------------------------ small helpers
    def _zone_now(self, z, t):
        c, R = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        ss, se = z["shrink_start"], z["shrink_end"]
        if t <= ss:
            return c[0], c[1], R
        if t >= se:
            return nc[0], nc[1], nr
        s = (t - ss) / max(1e-6, se - ss)
        return c[0] + (nc[0] - c[0]) * s, c[1] + (nc[1] - c[1]) * s, R + (nr - R) * s

    def _steer(self, x, y, dx, dy, look=8.0):
        """Rotate the desired direction until the first `look` metres are free (needs the map)."""
        nav = self.nav
        dx, dy = norm2(dx, dy)
        if nav is None or (dx == 0 and dy == 0):
            return dx, dy
        if nav.free_ray(x, y, dx, dy, look):
            return dx, dy
        a0 = math.atan2(dy, dx)
        for off in (0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.0, -2.0):
            a = a0 + off
            ux, uy = math.cos(a), math.sin(a)
            if nav.free_ray(x, y, ux, uy, look):
                return ux, uy
        return dx, dy

    def _pick_offer(self, offer):
        best, bi = 10 ** 6, 0
        for i, ch in enumerate(offer.get("choices", [])):
            cid = ch.get("id", "")
            r = UPGRADE_ORDER.index(cid) if cid in UPGRADE_ORDER else 500
            if r < best:
                best, bi = r, i
        return bi

    def _follow(self, x, y):
        """Pure pursuit on the planned polyline. Returns (dx, dy, remaining_m, cross_track_m)."""
        plan = self.plan
        P = plan["path"]
        if len(P) < 2:
            hx, hy = plan["H"]
            d = math.hypot(hx - x, hy - y)
            return (hx - x, hy - y, d, 0.0)
        best = None
        lo = max(0, self.pk - 1)
        hi = min(len(P) - 1, self.pk + 12)
        for k in range(lo, hi):
            ax, ay = P[k]
            bx, by = P[k + 1]
            sx, sy = bx - ax, by - ay
            L2 = sx * sx + sy * sy
            u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((x - ax) * sx + (y - ay) * sy) / L2))
            px, py = ax + sx * u, ay + sy * u
            d = math.hypot(x - px, y - py)
            if best is None or d < best[0]:
                best = (d, k, u, px, py)
        d, k, u, px, py = best
        self.pk = k
        # look ahead 10 m along the polyline
        remain = 10.0
        tx, ty = px, py
        kk = k
        cx, cy = px, py
        total = 0.0
        while kk < len(P) - 1:
            bx, by = P[kk + 1]
            seg = math.hypot(bx - cx, by - cy)
            if remain <= seg:
                f = remain / max(seg, 1e-9)
                tx, ty = cx + (bx - cx) * f, cy + (by - cy) * f
                remain = 0.0
                break
            remain -= seg
            cx, cy = bx, by
            tx, ty = bx, by
            kk += 1
        # remaining path length
        ax, ay = px, py
        for q in range(k + 1, len(P)):
            total += math.hypot(P[q][0] - ax, P[q][1] - ay)
            ax, ay = P[q]
        return (tx - x, ty - y, total, d)

    def _incoming_dodge(self, obs, me, enemies, t):
        """Returns (dx, dy, urgent) to leave a mortar impact point / laser line, or None."""
        x, y, z = me["pos"]
        gx = 0.0
        gy = 0.0
        urgent = False
        for p in obs.get("projectiles", []):
            if p.get("kind") != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            tt = 0.0
            while tt < 6.0:
                tt += 0.05
                vz -= self.gravity * 0.05
                px += vx * 0.05
                py += vy * 0.05
                pz += vz * 0.05
                gz = self.nav.gh(px, py) if self.nav else z
                if pz <= gz + 1.0:
                    break
            d = math.hypot(px - x, py - y)
            if d < 11.0:
                ux, uy = norm2(x - px, y - py)
                if ux == 0 and uy == 0:
                    a = self.rng.uniform(0, TAU)
                    ux, uy = math.cos(a), math.sin(a)
                gx += ux * (1.0 + (11.0 - d) / 11.0)
                gy += uy * (1.0 + (11.0 - d) / 11.0)
                urgent = urgent or tt < 1.2
        for e in enemies:
            if not e.get("charging"):
                continue
            ex, ey, _ = e["pos"]
            ax, ay = math.cos(e["aim"][0]), math.sin(e["aim"][0])
            rx, ry = x - ex, y - ey
            along = rx * ax + ry * ay
            if along < 0 or along > 320:
                continue
            cross = ax * ry - ay * rx
            if abs(cross) < 6.0:
                side = 1.0 if cross >= 0 else -1.0
                gx += -ay * side * 2.0
                gy += ax * side * 2.0
                urgent = True
        if gx == 0 and gy == 0:
            return None
        ux, uy = norm2(gx, gy)
        return ux, uy, urgent

    def _cover_dir(self, x, y, ex, ey, cx, cy, cr):
        nav = self.nav
        if nav is None:
            return None
        best = None
        for k in range(16):
            a = TAU * k / 16
            ux, uy = math.cos(a), math.sin(a)
            for r in (8.0, 14.0, 22.0, 30.0):
                px, py = x + ux * r, y + uy * r
                if math.hypot(px - cx, py - cy) > cr - 6.0:
                    continue
                if not nav.free_ray(x, y, ux, uy, r):
                    break
                if not nav.los(ex, ey, px, py):
                    if best is None or r < best[0]:
                        best = (r, ux, uy)
                    break
        return (best[1], best[2]) if best else None

    # ------------------------------------------------------------------ main decision
    def act(self, obs):
        t = obs["t"]
        self._t = t
        me = obs["self"]
        x, y, z = me["pos"]
        mid = me["id"]
        act = {"tick": obs["tick"]}
        nav = self.nav
        zone = obs["zone"]
        walk = me.get("stats", {}).get("walk_speed", 11.7)

        if obs.get("offer"):
            act["pick"] = self._pick_offer(obs["offer"])

        # ---- events
        for ev in obs.get("events", []):
            ty = ev.get("type")
            if ty == "damaged":
                self.last_damage_t = t
                if ev.get("by") is not None:
                    self.last_hit_by[ev["by"]] = t
                    self.last_bearing = ev.get("bearing")
                if ev.get("weapon") == "npc":
                    self.last_npc_hit = t
            elif ty == "take_failed":
                self.black[ev.get("id")] = t + 30.0
                self.loot = None
            elif ty == "took":
                self.loot = None

        for sd in obs.get("sounds", []):
            if sd.get("kind") in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar") and sd.get("age", 9.0) < 0.8:
                self.snd = (t, sd.get("bearing", 0.0), sd.get("dist", "far"), sd["kind"])

        # ---- adopt a finished plan
        pn = self.plan_new
        if pn is not None:
            self.plan_new = None
            if pn["ver"] == self.ver or (self.want is not None and pn.get("want") == self.want):
                self.plan = pn
                self.pk = 0

        # ---- zone
        cx, cy, cr = self._zone_now(zone, t)
        self._zplan = self._zone_now(zone, t + 4.0)
        nc, nr = zone["next_center"], zone["next_radius"]
        in_zone = me.get("in_zone", True)
        margin = cr - math.hypot(x - cx, y - cy)

        # ---- enemies
        enemies, npcs = [], []
        for m in obs.get("mechs", []):
            dx, dy, dz = m["pos"][0] - x, m["pos"][1] - y, m["pos"][2] - z
            m["_dh"] = math.hypot(dx, dy)
            m["_d"] = math.sqrt(dx * dx + dy * dy + dz * dz)
            if m["kind"] == "mech":
                enemies.append(m)
            else:
                npcs.append(m)
        for m in enemies + npcs:
            if m["_dh"] < 160:
                m["_vis"] = nav.los(x, y, m["pos"][0], m["pos"][1]) if nav else True
            else:
                m["_vis"] = False
        enemies.sort(key=lambda m: m["_d"])
        npcs.sort(key=lambda m: m["_d"])

        # ---- what do we want to be doing (plan request)
        key = (round(nc[0]), round(nc[1]), round(nr))
        drop = self._drop_target(obs, me, enemies, nc, nr, walk)
        want = ("drop", drop["id"]) if drop else ("hide", key)
        if nav is None:
            self.want = None          # keep asking until the map is loaded
        elif want != self.want:
            self.want = want
            if drop:
                self.tmpl = {"kind": "point", "goal": (drop["pos"][0], drop["pos"][1])}
            else:
                self.tmpl = {"kind": "hide", "nc": (nc[0], nc[1]), "nr": nr}
            self._issue(self.tmpl, x, y, want, False)

        # ---- base movement goal
        gdx = gdy = 0.0
        remaining = 0.0
        cross = 0.0
        have_plan = self.plan is not None
        arrived = False
        if not in_zone or margin < 2.0 and t < zone["shrink_end"] + 1:
            fx = fy = 0.0
            cross = 99.0
            if have_plan:
                fx, fy, remaining, cross = self._follow(x, y)
            if have_plan and cross < 10.0 and remaining > 3.0:
                gdx, gdy = norm2(fx, fy)          # the plan is zone-aware: it leads back inside
                self.mode = "zone-go"
            else:
                gdx, gdy = norm2(nc[0] - x, nc[1] - y) if math.hypot(nc[0] - x, nc[1] - y) > 3 else (0.0, 0.0)
                self.mode = "zone"
            if nav is not None:
                self._replan(t, x, y)
        elif have_plan:
            fx, fy, remaining, cross = self._follow(x, y)
            arrived = remaining < 3.0
            if not arrived:
                gdx, gdy = norm2(fx, fy)
            self.mode = "go" if not arrived else "hide"
            if cross > 14.0 and nav is not None and t > 1.0:     # pushed off the path: re-plan
                self._replan(t, x, y)
        else:
            dh = math.hypot(nc[0] - x, nc[1] - y)
            lim = max(10.0, nr * 0.55)
            if dh > lim:
                gdx, gdy = norm2(nc[0] - x, nc[1] - y)
            else:
                arrived = True
            self.mode = "go*"
        if gdx or gdy:
            gdx, gdy = self._steer(x, y, gdx, gdy)

        # ---- threats: decide whether (and whom) to fight
        fight, cmode = self._choose_fight(me, enemies, npcs, t)
        self.cmode = cmode
        vis_enemies = [e for e in enemies if e["_vis"] and e["_dh"] < CFG["CALM_R"]]

        # ---- dodge
        dodge = self._incoming_dodge(obs, me, enemies, t)

        # ---- calm? then farm resources / loot / supply drop
        calm = fight is None and not vis_enemies and t - self.last_damage_t > 3.0 and dodge is None and cmode != "recover"
        ldx = ldy = 0.0
        loot_active = False
        farm = None
        if calm and t < 380 and in_zone:
            if CFG["FARM"] and drop is None:
                farm = self._farm(obs, me, npcs, nc, nr, t, x, y)
            if farm is None:
                loot_active, ldx, ldy = self._loot(obs, me, nc, nr, drop, t, x, y)
        else:
            self.loot = None
            self.ft = None

        # ---- compose movement
        mx, my = gdx, gdy
        boost = False
        aim_yaw = None
        aim_pitch = 0.0
        fire = [False, False]
        if loot_active:
            mx, my = ldx, ldy
            self.mode = "loot"
        if farm is not None:
            mx, my, aim_yaw, aim_pitch, fire = farm["mx"], farm["my"], farm["yaw"], farm["pitch"], farm["fire"]
            self.mode = "farm"
        if fight is not None:
            mx, my, aim_yaw, aim_pitch, fire, flee = self._duel(obs, me, fight, enemies, (gdx, gdy), t, cmode)
            self.mode = "flee" if flee else "duel"
            boost = flee and me["energy"] > 30
        else:
            nearest = enemies[0] if enemies and enemies[0]["_vis"] and enemies[0]["_dh"] < 130 else None
            if nearest is not None:
                aim_yaw = math.atan2(nearest["pos"][1] - y, nearest["pos"][0] - x)     # face it (rear armor)
                if cmode == "recover":
                    cov = self._cover_dir(x, y, nearest["pos"][0], nearest["pos"][1], cx, cy, cr)
                    if cov:
                        mx, my = cov[0] * 1.2 + gdx * 0.3, cov[1] * 1.2 + gdy * 0.3
                    else:
                        ux, uy = norm2(nearest["pos"][0] - x, nearest["pos"][1] - y)
                        mx, my = gdx * 0.8 - ux * 0.6, gdy * 0.8 - uy * 0.6
                    boost = me["energy"] > 40
                    self.mode = "recover"
            elif t - self.last_damage_t < 3.0 and self.last_bearing is not None:
                aim_yaw = self.last_bearing          # face where it came from (rear armor)
            elif CFG["SOUND_AIM"] and self.snd is not None and t - self.snd[0] < 3.0 and not (gdx or gdy):
                aim_yaw = self.snd[1]                # standing still: look toward the noise (200 m wedge instead of 45 m)
        if dodge is not None:
            mx, my = mx + dodge[0] * 2.0, my + dodge[1] * 2.0
            if dodge[2] and me["energy"] > 25:
                boost = True
            self.mode = "dodge"
        if not in_zone and me["energy"] > 12:
            boost = True

        # ---- stuck detection
        self.hist.append((t, x, y))
        if math.hypot(self.last_cmd[0], self.last_cmd[1]) > 0.2:
            if self.moving_since is None:
                self.moving_since = t
        else:
            self.moving_since = None
        if t < self.jitter_until:
            mx, my = mx + self.jitter[0], my + self.jitter[1]
        elif (self.mode in ("go", "go*", "zone", "zone-go", "loot", "sneak", "farm") and (mx or my) and self.moving_since is not None
              and t - self.moving_since >= 2.4 and me.get("grounded", True)):
            old = None
            for (th, hx, hy) in self.hist:           # newest sample that is >= 2.4 s old
                if th <= t - 2.4:
                    old = (hx, hy)
            if old is not None and math.hypot(x - old[0], y - old[1]) < 2.5:
                ux, uy = norm2(mx, my)
                if nav is not None:
                    nav.learn_block(x + ux * 5.0, y + uy * 5.0)
                    nav.learn_block(x + ux * 9.0, y + uy * 9.0)
                    self.req_t = -99.0            # a stuck re-plan must not be suppressed
                    self._replan(t, x, y)
                a = math.atan2(uy, ux) + self.rng.choice((1.4, -1.4))
                self.jitter = (math.cos(a) * 1.2, math.sin(a) * 1.2)
                self.jitter_until = t + 1.0
                self.moving_since = None
        n = math.hypot(mx, my)
        if n > 1.0:
            mx, my = mx / n, my / n
        # ---- stealth walking: below step_speed the mech makes no footstep noise. Only when nothing is around,
        # we are comfortably inside the zone and can still arrive before the next shrink starts.
        if (self.mode == "go" and have_plan and not boost and calm and t >= self.jitter_until and margin > 20.0
                and not any(e["_vis"] for e in enemies) and t - self.last_damage_t > 8.0
                and t < zone["shrink_start"] and (mx or my)):
            quiet = max(1.0, self.step_speed - 0.3)
            if remaining / quiet + 8.0 < zone["shrink_start"] - t:
                cap = quiet / max(walk, 1.0)
                n = math.hypot(mx, my)
                if n > cap:
                    mx, my = mx / n * cap, my / n * cap
                self.mode = "sneak"
        self.last_cmd = (mx, my)
        act["move"] = [fin(mx), fin(my)]
        act["boost"] = bool(boost)

        # ---- aim
        if aim_yaw is None:
            if mx or my:
                aim_yaw = math.atan2(my, mx)
            elif have_plan and self.plan.get("expo"):
                aim_yaw = self.plan["open_dir"] + 1.05 * math.sin(t * 0.9)
            else:
                aim_yaw = me["aim"][0]
        pitch = max(self.min_pitch, min(self.max_pitch, aim_pitch))
        act["aim"] = [fin(aim_yaw), fin(pitch)]
        act["fire"] = [bool(fire[0]), bool(fire[1])]

        # ---- gun housekeeping
        w0 = me["weapons"][0] if me.get("weapons") else None
        if w0 and w0.get("kind") == "gun":
            mag = w0["stats"].get("mag", 12)
            if w0.get("ammo", mag) <= 0 and w0.get("reload", 0) <= 0:
                act["reload"] = True
            elif fight is None and not fire[0] and w0.get("ammo", mag) < 0.5 * mag and w0.get("reload", 0) <= 0 and (self.mode != "farm" or w0.get("ammo", mag) <= 3):
                act["reload"] = True

        # ---- take weapons
        tk = self._take(obs, me, t)
        if tk is not None:
            act.update(tk)

        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} a{obs['alive']}")
        return act

    # ------------------------------------------------------------------ combat
    def _lead(self, me, tgt, speed):
        mx, my, mz = me["pos"]
        mh = me.get("stats", {}).get("muzzle_height", 7.0)
        tx, ty, tz = tgt["pos"]
        vx, vy, vz = tgt.get("vel", [0, 0, 0])
        if tgt["kind"] == "mech":
            tc = tz + 4.5 * (1 + 0.05 * tgt.get("cubes", 0))
        else:
            tc = tz + 1.75
        oz = mz + mh
        d = math.sqrt((tx - mx) ** 2 + (ty - my) ** 2 + (tc - oz) ** 2)
        th = 0.0
        for _ in range(3):
            th = min(1.0, d / max(speed, 1.0))
            px, py, pz = tx + vx * th, ty + vy * th, tc + vz * th * 0.0
            d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - oz) ** 2)
        yaw = math.atan2(py - my, px - mx)
        pitch = math.atan2(pz - oz, math.hypot(px - mx, py - my))
        return yaw, pitch, d

    # ------------------------------------------------------------------ combat (utility model)
    @staticmethod
    def _enemy_dps(e):
        """Rough damage per second an enemy mech can put on us (accuracy-discounted)."""
        cm = 1.0 + 0.04 * min(e.get("cubes", 0) or 0, 10)
        w = e.get("weapons") or []
        d = e.get("_dh", 60.0)
        dps = 187.0 * (0.55 if d < 60 else 0.4)
        if "lightning" in w and d < 45:
            dps += 240.0 * 0.7
        if "laser" in w and d < 220:
            dps += 131.0 * 0.5
        if "mortar" in w and d < 160:
            dps += 120.0 * 0.3
        return dps * cm

    @staticmethod
    def _my_dps(me, d):
        cm = 1.0 + 0.04 * min(me.get("cubes", 0) or 0, 10)
        ws = me.get("weapons") or []
        dps = 0.0
        w0 = ws[0] if ws else None
        if w0 and w0.get("kind") == "gun":
            st = w0.get("stats", {})
            acc = max(0.3, min(0.85, 0.9 - d / 250.0))
            dps += st.get("damage", 45.0) / max(st.get("cooldown", 0.24), 0.05) * acc
        w1 = ws[1] if len(ws) > 1 else None
        if w1:
            if w1.get("kind") == "lightning" and d < 42:
                dps += 240.0 * 0.8
            elif w1.get("kind") == "laser" and d < 250:
                dps += 131.0 * 0.55
        return max(dps * cm, 30.0)

    def _choose_fight(self, me, enemies, npcs, t):
        """Returns (target, mode). mode: none | duel | flee | recover."""
        hp, mhp = me["hp"], max(me["max_hp"], 1.0)
        frac = hp / mhp
        vis = [e for e in enemies if e["_vis"] and e["_dh"] <= 170]
        near = [e for e in vis if e["_dh"] <= 115]
        D = sum(self._enemy_dps(e) for e in near)
        best = None
        for e in vis:
            d = e["_dh"]
            ehp = e.get("hp") or 1400.0
            emax = e.get("max_hp") or 1400.0
            provoked = (t - self.last_hit_by.get(e["id"], -99.0)) < 4.0
            weak = ehp <= 0.3 * emax and d < 130
            close = d < CFG["CLOSE"]
            tk = ehp / self._my_dps(me, d)
            td = hp / max(D, 60.0)
            fav = tk < CFG["FAVOR"] * td
            keep = e["id"] == self.fight_id and d <= 140
            opp = CFG["OPP"] and d <= CFG["OPP_RANGE"] and fav and frac > 0.5 and len(near) <= CFG["OPP_MAXN"]
            if not (provoked or close or weak or opp or keep):
                continue
            score = d - (40 if provoked else 0) - (30 if weak else 0) - (15 if keep else 0)
            if best is None or score < best[0]:
                best = (score, e, tk, td, provoked)
        if best is None:
            self.fight_id = None
            if t < self.recover_until and frac < 0.85:
                return None, "recover"
            if t - self.last_npc_hit < 4.0:
                for n in npcs:
                    if n["_vis"] and n["_dh"] < 60:
                        return n, "duel"
            return None, "none"
        _, e, tk, td, provoked = best
        thr = CFG["RETREAT"] + 0.07 * max(0, len(near) - 1)
        winning = tk < 0.5 * td
        if CFG["RECOVER"] and frac < thr and not winning and e["kind"] == "mech":
            self.recover_until = t + 7.0
            if e["_dh"] > CFG["RECOVER_RANGE"]:
                self.fight_id = None
                return None, "recover"          # out of its reach: break contact, let the 45 HP/s regeneration run
            return e, "flee"                    # in its reach: keep shooting while backing toward cover
        if not CFG["RECOVER"] and frac < 0.35 and e["kind"] == "mech":
            return e, "flee"
        self.fight_id = e["id"]
        return e, "duel"

    def _pref_range(self, me, tgt):
        ws = me.get("weapons") or []
        w1 = ws[1] if len(ws) > 1 else None
        ew = tgt.get("weapons") or []
        if w1 and w1.get("kind") == "lightning":
            pref = 24.0
            if "mortar" in ew:
                pref = 14.0
        elif w1 and w1.get("kind") == "laser":
            pref = 80.0
            if "lightning" in ew:
                pref = 70.0
        else:
            pref = 48.0
        if "lightning" in ew and not (w1 and w1.get("kind") == "lightning"):
            pref = max(pref, 62.0)           # stay out of its 45 m arc
        return pref

    def _duel(self, obs, me, tgt, enemies, goal, t, cmode):
        x, y, z = me["pos"]
        w = me["weapons"]
        speed = 252.0
        if w and w[0] and w[0].get("kind") == "gun":
            speed = w[0]["stats"].get("speed", 252.0)
        yaw, pitch, d = self._lead(me, tgt, speed)
        pitch = max(self.min_pitch, min(self.max_pitch, pitch))
        dh = tgt["_dh"]
        ayaw, apit = me["aim"]
        yerr = abs(angdiff(ayaw, yaw))
        perr = abs(apit - pitch)
        tol = max(0.025, min(0.12, math.atan2(2.2, max(dh, 1.0))))
        aligned = yerr < tol and perr < tol * 1.6
        crowd = sum(1 for e in enemies if e["_vis"] and e["_dh"] < 40)
        fire = [False, False]
        for slot in (0, 1):
            wp = w[slot] if slot < len(w) else None
            if not wp:
                continue
            kind = wp.get("kind")
            st = wp.get("stats", {})
            if kind == "gun":
                if wp.get("ammo", 1) > 0 and wp.get("reload", 0) <= 0 and dh <= st.get("range", 230) * 0.85 and aligned:
                    fire[slot] = True
            elif kind == "laser":
                ch = wp.get("charge", 0.0)
                full = st.get("charge", 0.75)
                if ch > 0:
                    fire[slot] = ch < full - 0.05 and yerr < tol * 3        # release at full charge
                elif wp.get("cooldown", 0) <= 0.05 and dh <= st.get("range", 280) * 0.9 and yerr < tol * 2.5 and crowd == 0 and dh > 20:
                    fire[slot] = True
            elif kind == "lightning":
                cone = math.radians(st.get("cone_deg", 30.0)) / 2
                if dh <= st.get("range", 45) * 0.92 and yerr < cone * 0.6 and me.get("energy", 0) >= 6:
                    fire[slot] = True
        # ---- movement: hold the preferred range, strafe unpredictably, stay inside the zone, keep clear of walls
        ex, ey = tgt["pos"][0] - x, tgt["pos"][1] - y
        ux, uy = norm2(ex, ey)
        if t > self.strafe_until:
            self.strafe_sign = self.rng.choice((-1, 1))
            self.strafe_until = t + self.rng.uniform(0.6, 1.5)
        flee = cmode == "flee"
        pref = self._pref_range(me, tgt) if tgt["kind"] == "mech" else 30.0
        radial = max(-1.0, min(1.0, (dh - pref) / 18.0)) * 0.9
        if flee:
            radial = -0.9
        tang = self.strafe_sign * 0.9
        mx, my = ux * radial - uy * tang, uy * radial + ux * tang
        if flee:
            cov = self._cover_dir(x, y, tgt["pos"][0], tgt["pos"][1], *self._zplan)
            if cov:
                mx, my = mx * 0.3 + cov[0] * 1.2, my * 0.3 + cov[1] * 1.2
        else:
            mx += goal[0] * 0.2
            my += goal[1] * 0.2
        # zone: never fight our way out of the circle that will exist in ~4 s
        zx, zy, zr = self._zplan
        marg = zr - math.hypot(x - zx, y - zy)
        if marg < CFG["ZONE_BUF"]:
            push = min(1.0, (CFG["ZONE_BUF"] - marg) / CFG["ZONE_BUF"])
            ix, iy = norm2(zx - x, zy - y)
            mx += ix * 1.8 * push
            my += iy * 1.8 * push
        mx, my = self._steer(x, y, mx, my, look=6.0)
        if self.nav is not None and not self.nav.free_ray(x, y, *norm2(mx, my), 6.0):
            self.strafe_sign = -self.strafe_sign
        return mx, my, yaw, pitch, fire, flee

    # ------------------------------------------------------------------ resource farming
    def _farm(self, obs, me, npcs, nc, nr, t, x, y):
        """Pick a crate / drone / cube / weapon worth a short detour and return the action for it (or None)."""
        z = me["pos"][2]
        ws = me.get("weapons") or []
        slot1_free = len(ws) > 1 and ws[1] is None
        if me["hp"] < CFG["FARM_HP"] * me["max_hp"]:
            self.ft = None
            return None
        early = t < CFG["FARM_UNTIL"]
        cands = []

        def ok_place(p, margin):
            return math.hypot(p[0] - nc[0], p[1] - nc[1]) < nr - margin and abs(zof(p, z) - z) <= 7.0

        if slot1_free:
            for p in obs.get("pickups", []):
                if p["kind"] not in ("lightning", "laser") or self.black.get(p["id"], 0) > t:
                    continue
                dh = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
                if dh <= 90 and ok_place(p["pos"], 8):
                    val = 7.0 if p["kind"] == "lightning" else 5.5
                    cands.append((val / (2.0 + dh / 11.0), "pick", p["id"], p["pos"], dh))
        for c in obs.get("cubes", []):
            if self.black.get(c["id"], 0) > t:
                continue
            dh = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
            if dh <= (60 if early else 30) and ok_place(c["pos"], 8):
                cands.append((2.6 / (2.0 + dh / 11.0), "cube", c["id"], c["pos"], dh))
        if early:
            for n in npcs:
                if n["hp"] <= 0 or self.black.get(n["id"], 0) > t or not n["_vis"]:
                    continue
                dh = n["_dh"]
                if dh <= 75 and ok_place(n["pos"], 6):
                    cands.append((4.4 / (2.0 + dh / 11.0 + 3.0), "npc", n["id"], n["pos"], dh))
            for c in obs.get("crates", []):
                if c.get("hp", 0) <= 0 or self.black.get(c["id"], 0) > t:
                    continue
                dh = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if dh <= 70 and ok_place(c["pos"], 6) and self.nav is not None and self.nav.los(x, y, c["pos"][0], c["pos"][1]):
                    val = 3.2 + (1.2 if slot1_free else 0.0)
                    cands.append((val / (2.0 + dh / 11.0 + 2.6), "crate", c["id"], c["pos"], dh))
        if not cands:
            self.ft = None
            return None
        cur = self.ft["id"] if self.ft else None
        best = None
        for sc, kind, cid, pos, dh in cands:
            if cid == cur:
                sc *= 1.5
            if best is None or sc > best[0]:
                best = (sc, kind, cid, pos, dh)
        _, kind, cid, pos, dh = best
        if self.ft is None or self.ft["id"] != cid:
            self.ft = {"id": cid, "best": dh, "since": t, "kind": kind}
        elif dh < self.ft["best"] - 1.5:
            self.ft["best"], self.ft["since"] = dh, t
        elif t - self.ft["since"] > (6.0 if kind in ("npc", "crate") else 3.5) and not self.ft.get("shooting"):
            self.black[cid] = t + 45.0
            self.ft = None
            return None
        ux, uy = norm2(pos[0] - x, pos[1] - y)
        res = {"mx": 0.0, "my": 0.0, "yaw": math.atan2(pos[1] - y, pos[0] - x), "pitch": 0.0, "fire": [False, False]}
        stand = CFG["FARM_STAND"]
        self.ft["shooting"] = False
        if kind in ("pick", "cube"):
            if dh > (1.5 if kind == "cube" else 2.6):
                sx, sy = self._steer(x, y, ux, uy, look=6.0)
                if self.nav is not None and not self.nav.free_ray(x, y, ux, uy, min(dh, 10.0)) and dh > 6:
                    pass
                res["mx"], res["my"] = sx, sy
            return res
        # npc / crate: close to firing range with a clear line, then shoot until it breaks
        tgt = {"kind": "npc" if kind == "npc" else "crate", "pos": pos, "vel": [0, 0, 0]}
        if kind == "npc":
            for n in npcs:
                if n["id"] == cid:
                    tgt = n
                    break
        yaw, pitch, d = self._lead(me, tgt, 252.0)
        pitch = max(self.min_pitch, min(self.max_pitch, pitch))
        res["yaw"], res["pitch"] = yaw, pitch
        los = self.nav.los(x, y, pos[0], pos[1]) if self.nav is not None else True
        if dh > stand or not los:
            sx, sy = self._steer(x, y, ux, uy, look=6.0)
            res["mx"], res["my"] = sx, sy
        w0 = ws[0] if ws else None
        ayaw, apit = me["aim"]
        tol = max(0.03, min(0.12, math.atan2(2.4, max(dh, 1.0))))
        if los and dh <= 62:
            if w0 and w0.get("kind") == "gun" and w0.get("ammo", 1) > 0 and w0.get("reload", 0) <= 0 \
                    and abs(angdiff(ayaw, yaw)) < tol and abs(apit - pitch) < tol * 1.6:
                res["fire"][0] = True
                self.ft["shooting"] = True
            w1 = ws[1] if len(ws) > 1 else None
            if w1 and w1.get("kind") == "lightning" and dh < 38 and abs(angdiff(ayaw, yaw)) < 0.2 and me.get("energy", 0) > 40:
                res["fire"][1] = True
                self.ft["shooting"] = True
        return res

    # ------------------------------------------------------------------ loot
    def _drop_target(self, obs, me, enemies, nc, nr, walk):
        x, y, _ = me["pos"]
        t = obs["t"]
        if me["hp"] < 0.25 * me["max_hp"] or t - self.last_damage_t < 2.0:
            return None
        for d in obs.get("drops", []):
            if d.get("holder") not in (None, me["id"]):
                continue
            px, py = d["pos"][0], d["pos"][1]
            dd = math.hypot(px - x, py - y)
            if dd > CFG["DROP_REACH"] or d["lands_in"] > dd / (walk * 0.85) + 3.0:
                continue
            if math.hypot(px - nc[0], py - nc[1]) > nr - 6.0:
                continue
            if any(math.hypot(e["pos"][0] - px, e["pos"][1] - py) < 55 for e in enemies):
                continue
            return {"id": d["id"], "pos": d["pos"], "radius": d.get("radius", 8.0), "dd": dd}
        return None

    def _loot(self, obs, me, nc, nr, drop, t, x, y):
        """Returns (active, dx, dy). Drops are handled by the planner (we only hold still in the ring)."""
        if drop is not None:
            if drop["dd"] < drop["radius"] - 2.0:
                return True, 0.0, 0.0            # capture: stand still in the ring
            return False, 0.0, 0.0
        have_slot = me["weapons"][1] is None if len(me.get("weapons", [])) > 1 else False
        best = None
        if have_slot:
            for p in obs.get("pickups", []):
                if p["kind"] not in ("laser", "lightning") or self.black.get(p["id"], 0) > t:
                    continue
                dh = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
                if dh > 38 or abs(zof(p["pos"], me["pos"][2]) - me["pos"][2]) > 5:
                    continue
                if math.hypot(p["pos"][0] - nc[0], p["pos"][1] - nc[1]) > nr - 8:
                    continue
                score = dh - (12 if p["kind"] == "laser" else 0)
                if best is None or score < best[0]:
                    best = (score, p["id"], p["pos"], p["kind"])
        if best is None:
            for c in obs.get("cubes", []):
                if self.black.get(c["id"], 0) > t:
                    continue
                dh = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if dh > 26 or abs(zof(c["pos"], me["pos"][2]) - me["pos"][2]) > 5:
                    continue
                if math.hypot(c["pos"][0] - nc[0], c["pos"][1] - nc[1]) > nr - 8:
                    continue
                if best is None or dh < best[0]:
                    best = (dh, c["id"], c["pos"], "cube")
        if best is None:
            self.loot = None
            return False, 0.0, 0.0
        _, lid, lpos, kind = best
        dh = math.hypot(lpos[0] - x, lpos[1] - y)
        if self.loot is None or self.loot["id"] != lid:
            self.loot = {"id": lid, "best": dh, "since": t}
        elif dh < self.loot["best"] - 1.5:
            self.loot["best"], self.loot["since"] = dh, t
        elif t - self.loot["since"] > 3.0:
            self.black[lid] = t + 40.0
            self.loot = None
            return False, 0.0, 0.0
        if dh < 1.0:
            return True, 0.0, 0.0
        dx, dy = self._steer(x, y, lpos[0] - x, lpos[1] - y, look=6.0)
        if self.nav is not None and not self.nav.free_ray(x, y, *norm2(lpos[0] - x, lpos[1] - y), min(dh, 12.0)):
            self.black[lid] = t + 40.0
            self.loot = None
            return False, 0.0, 0.0
        return True, dx, dy

    def _take(self, obs, me, t):
        ws = me.get("weapons", [])
        if len(ws) < 2 or ws[1] is not None:
            return None
        x, y, z = me["pos"]
        best = None
        for p in obs.get("pickups", []):
            if p["kind"] not in ("laser", "lightning") or self.black.get(p["id"], 0) > t:
                continue
            dh = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
            if dh <= 3.6 and abs(zof(p["pos"], z) - z) <= 3.6:
                score = dh - (6 if p["kind"] == "lightning" else 0)
                if best is None or score < best[0]:
                    best = (score, p["id"])
        return {"take": best[1]} if best else None


if __name__ == "__main__":
    mf.run(Ghost)
