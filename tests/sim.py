#!/usr/bin/env python3
"""Tiny kinematic match simulator for exercising the bot's logic (NOT the real game).

What it models: walking with acceleration, wall collision against the map's solid raster (sliding), the
shrinking zone with damage and HP regen, optional scripted enemies that hunt/shoot when they see the
bot, mortar shells, an upgrade offer, a laser pickup, cubes and supply drops ("--chaos").
What it does not model: real weapon ballistics, vision wedges, sounds, other bots' intelligence.
Use it to catch crashes, deadlocks, zone deaths and timing problems -- not to estimate the score.

    python3 tests/sim.py --map /tmp/arena [--chaos] [--enemies 3] [--seed 1] [--seconds 480]
"""
import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import mechfight as mf  # noqa: E402  (official wrapper, copied verbatim into src/
from nav import NavMap  # noqa: E402
import ghost_bot  # noqa: E402


def load_config():
    with open(os.path.join(HERE, "hello_config.json")) as f:
        return json.load(f)


class Zone:
    def __init__(self, cfg, rng):
        z = cfg["zone"]
        self.phases = z["phases"]
        self.circles = [(z["start_center"][0], z["start_center"][1], z["start_radius"])]
        c = self.circles[0]
        for ph in self.phases:
            R = ph["radius"]
            maxoff = max(0.0, (c[2] - R) * 0.9)
            for _ in range(200):
                a, r = rng.uniform(0, 2 * math.pi), rng.uniform(0, maxoff)
                nc = (c[0] + r * math.cos(a), c[1] + r * math.sin(a), R)
                if -440 < nc[0] < 480 and -270 < nc[1] < 330:
                    break
            c = nc
            self.circles.append(c)
        self.starts = []
        t = 0.0
        for ph in self.phases:
            self.starts.append(t)
            t += ph["hold_s"] + ph["shrink_s"]
        self.end = t

    def at(self, t):
        for k, ph in enumerate(self.phases):
            t0 = self.starts[k]
            if t < t0 + ph["hold_s"] + ph["shrink_s"]:
                a, b = self.circles[k], self.circles[k + 1]
                ss, se = t0 + ph["hold_s"], t0 + ph["hold_s"] + ph["shrink_s"]
                if t <= ss:
                    cur = a
                elif t >= se:
                    cur = b
                else:
                    s = (t - ss) / (se - ss)
                    cur = (a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s, a[2] + (b[2] - a[2]) * s)
                return cur, {"center": [a[0], a[1]], "radius": a[2], "next_center": [b[0], b[1]], "next_radius": b[2],
                             "shrink_start": ss, "shrink_end": se, "dps": ph["dps"]}
        last = self.circles[-1]
        return last, {"center": [last[0], last[1]], "radius": last[2], "next_center": [last[0], last[1]],
                      "next_radius": last[2], "shrink_start": self.end, "shrink_end": self.end, "dps": self.phases[-1]["dps"]}


class Sim:
    def __init__(self, nav, cfg, seed, chaos=False, n_enemies=0, spawn=None):
        self.nav, self.cfg, self.rng, self.chaos = nav, cfg, random.Random(seed), chaos
        self._free_cells = None
        self.zone = Zone(cfg, self.rng)
        self.t = 0.0
        self.tick = 0
        z0 = self.zone.circles[0]
        while True:
            a, r = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(0, 0.8 * z0[2])
            x, y = z0[0] + r * math.cos(a), z0[1] + r * math.sin(a)
            if spawn is not None:
                x, y = spawn
            if -480 < x < 520 and -300 < y < 360 and not nav.block_at(x, y):
                if spawn is not None or self._open_spawn(x, y):
                    break
        self.pos = [x, y]
        self.vel = [0.0, 0.0]
        self.noisy_s = 0.0           # seconds spent moving faster than the footstep threshold (5 m/s)
        self.aim = [0.0, 0.0]
        self.hp = cfg["mech"]["hp"]
        self.max_hp = self.hp
        self.energy = 100.0
        self.last_dmg_t = -99.0
        self.zone_damage = 0.0
        self.enemy_damage = 0.0
        self.ammo, self.reload_left, self.gun_cd = 12, 0.0, 0.0
        self.weapons = [self._gun(), None]
        self.cubes_owned = 0
        self.events = []
        self.offer = None
        self.upgrades = []
        self.enemies = []
        for k in range(n_enemies):
            for _ in range(200):
                a, r = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(0, 0.8 * z0[2])
                ex, ey = z0[0] + r * math.cos(a), z0[1] + r * math.sin(a)
                if -480 < ex < 520 and -300 < ey < 360 and not nav.block_at(ex, ey) and math.hypot(ex - x, ey - y) > 130:
                    break
            self.enemies.append({"id": 5 + k, "pos": [ex, ey], "vel": [0.0, 0.0], "hp": 1400.0, "wp": None, "cool": 0.0})
        self.pickups = [{"id": 8001, "kind": "laser", "pos": [x + 20, y + 5, 0.0]}] if chaos else []
        self.cubes = [{"id": 5001, "pos": [x - 12, y + 8, 0.0]}] if chaos else []
        self.projectiles = []
        self.drops = []
        self.dead = False
        self.min_margin = 1e9
        self.fired = 0
        self.hits_taken = 0

    def _open_spawn(self, x, y):
        """Reject spawns sealed inside a courtyard (the synthetic map has some); the real arena should not."""
        s = self.nav.nearest_free(*self.nav.cell_of(x, y), maxr=2)
        if s is None:
            return False
        dist, _ = self.nav.dijkstra(*s)
        reach = sum(1 for d in dist if d < 1e17)
        if self._free_cells is None:
            nx = self.nav.nx
            self._free_cells = sum(1 for k in range(nx * self.nav.ny) if not self.nav.block[k])
        return reach >= 0.5 * self._free_cells

    def _gun(self):
        return {"kind": "gun", "cooldown": 0.0, "ammo": 12, "reload": 0.0,
                "stats": dict(self.cfg["weapons"]["gun"])}

    def ground(self, x, y):
        return self.nav.gh(x, y)

    def _collide(self, nx_, ny_):
        r = 2.0
        for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            if self.nav_solid(nx_ + dx, ny_ + dy):
                return True
        return False

    def nav_solid(self, x, y):
        n = self.nav
        i, j = int((x - n.ox) / 2.0), int((y - n.oy) / 2.0)
        if 0 <= i < n.fnx and 0 <= j < n.fny:
            return bool(n.solid[j * n.fnx + i])
        return True

    def visible(self, a, b):
        return self.nav.los(a[0], a[1], b[0], b[1])

    def obs(self):
        t = self.t
        cur, zdict = self.zone.at(t)
        x, y = self.pos
        z = self.ground(x, y)
        inz = math.hypot(x - cur[0], y - cur[1]) <= cur[2]
        mechs = []
        for e in self.enemies:
            if e["hp"] <= 0:
                continue
            d = math.hypot(e["pos"][0] - x, e["pos"][1] - y)
            if d < 45 or (d < 150 and self.visible(self.pos, e["pos"])):
                mechs.append({"id": e["id"], "kind": "mech", "pos": [e["pos"][0], e["pos"][1], self.ground(*e["pos"])],
                              "vel": [e["vel"][0], e["vel"][1], 0.0], "aim": [math.atan2(y - e["pos"][1], x - e["pos"][0]), 0.0],
                              "hp": e["hp"], "max_hp": 1400.0, "cubes": 0, "weapons": ["gun"], "charging": False})
        events, self.events = self.events, []
        drops = []
        if self.chaos:
            for k, dt in enumerate(self.cfg["drops"]["times"]):
                if dt <= t < dt + 60:
                    land = dt + self.cfg["drops"]["fall_s"]
                    c = self.zone.circles[min(k + 1, len(self.zone.circles) - 1)]
                    drops.append({"id": 20000 + k, "pos": [c[0] + 5, c[1] + 5, 0.0], "radius": 8, "lands_in": max(0.0, land - t),
                                  "capture_s": 4, "progress": 0, "holder": None})
        ws = []
        for w in self.weapons:
            if w is None:
                ws.append(None)
            else:
                w = dict(w)
                if w["kind"] == "gun":
                    w["ammo"], w["reload"], w["cooldown"] = self.ammo, self.reload_left, self.gun_cd
                ws.append(w)
        o = {"type": "obs", "tick": self.tick, "t": round(t, 2), "alive": 1 + sum(1 for e in self.enemies if e["hp"] > 0),
             "self": {"id": 3, "pos": [x, y, z], "vel": [self.vel[0], self.vel[1], 0.0], "aim": list(self.aim), "hp": self.hp,
                      "max_hp": self.max_hp, "energy": self.energy, "max_energy": 100, "grounded": True, "cubes": self.cubes_owned,
                      "upgrades": self.upgrades, "weapons": ws, "in_zone": inz,
                      "stats": {"walk_speed": 11.7, "boost_speed": 19.5, "turn_rate": 4, "muzzle_height": 7, "eye_height": 8}},
             "mechs": mechs, "projectiles": [dict(p) for p in self.projectiles], "crates": [], "cubes": list(self.cubes),
             "pickups": list(self.pickups), "sounds": [], "beams": [], "events": events, "zone": zdict,
             "offer": self.offer, "drops": drops}
        return o

    def step(self, a):
        dt = 0.1
        cfg = self.cfg
        # offer / pick
        if self.chaos and self.offer is None and abs(self.t - 40.0) < 0.05:
            self.offer = {"id": 1, "expires": self.t + 5, "choices": [{"id": "gun_rate", "name": "", "desc": ""},
                                                                          {"id": "armor", "name": "", "desc": ""},
                                                                          {"id": "nanites", "name": "", "desc": ""}]}
        if self.offer is not None and "pick" in a:
            self.upgrades.append(self.offer["choices"][int(a["pick"])]["id"])
            self.offer = None
        # aim slew
        ty, tp = a.get("aim", self.aim)
        dy = (ty - self.aim[0] + math.pi) % (2 * math.pi) - math.pi
        self.aim[0] += max(-4 * dt, min(4 * dt, dy))
        self.aim[1] += max(-3 * dt, min(3 * dt, tp - self.aim[1]))
        # move
        mv = a.get("move", [0, 0])
        mag = min(1.0, math.hypot(*mv))
        sp = 19.5 if (a.get("boost") and self.energy > 0 and mag > 0) else 11.7
        if a.get("boost") and mag > 0 and self.energy > 0:
            self.energy = max(0.0, self.energy - 17 * dt)
        else:
            self.energy = min(100.0, self.energy + 18 * dt)
        want = (mv[0] / mag * mag * sp if mag else 0.0, mv[1] / mag * mag * sp if mag else 0.0) if mag else (0.0, 0.0)
        for k in (0, 1):
            d = want[k] - self.vel[k]
            self.vel[k] += max(-58.5 * dt, min(58.5 * dt, d))
        if math.hypot(self.vel[0], self.vel[1]) > 5.0:
            self.noisy_s += dt
        nx_, ny_ = self.pos[0] + self.vel[0] * dt, self.pos[1] + self.vel[1] * dt
        if not self._collide(nx_, ny_):
            self.pos = [nx_, ny_]
        elif not self._collide(nx_, self.pos[1]):
            self.pos[0] = nx_
            self.vel[1] = 0.0
        elif not self._collide(self.pos[0], ny_):
            self.pos[1] = ny_
            self.vel[0] = 0.0
        else:
            self.vel = [0.0, 0.0]
        # pickups
        for p in list(self.pickups):
            if a.get("take") == p["id"] and math.hypot(p["pos"][0] - self.pos[0], p["pos"][1] - self.pos[1]) <= 4.0 and self.weapons[1] is None:
                self.weapons[1] = {"kind": p["kind"], "cooldown": 0.0, "charge": 0.0, "stats": dict(cfg["weapons"][p["kind"]])}
                self.events.append({"type": "took", "id": p["id"], "kind": p["kind"], "slot": 1})
                self.pickups.remove(p)
        for c in list(self.cubes):
            if math.hypot(c["pos"][0] - self.pos[0], c["pos"][1] - self.pos[1]) <= 3.5:
                self.cubes.remove(c)
                self.cubes_owned += 1
                self.events.append({"type": "pickup", "cubes": self.cubes_owned})
        # my gun
        self.gun_cd = max(0.0, self.gun_cd - dt)
        if self.reload_left > 0:
            self.reload_left = max(0.0, self.reload_left - dt)
            if self.reload_left == 0:
                self.ammo = 12
        elif a.get("reload") and self.ammo < 12:
            self.reload_left = 2.4
        fire = a.get("fire", [False, False])
        if fire[0] and self.ammo > 0 and self.reload_left == 0 and self.gun_cd <= 0:
            shots = 0
            while self.gun_cd <= dt and self.ammo > 0 and shots < 2:
                self.ammo -= 1
                shots += 1
                self.gun_cd += 0.24
                self.fired += 1
                for e in self.enemies:
                    if e["hp"] <= 0:
                        continue
                    ex, ey = e["pos"][0] - self.pos[0], e["pos"][1] - self.pos[1]
                    d = math.hypot(ex, ey)
                    if d < 230 and abs((math.atan2(ey, ex) - self.aim[0] + math.pi) % (2 * math.pi) - math.pi) < math.atan2(2.5, d) \
                            and self.visible(self.pos, e["pos"]):
                        e["hp"] -= 45 * 0.5
            if self.ammo == 0:
                self.reload_left = 2.4
        # enemies
        for e in self.enemies:
            if e["hp"] <= 0:
                continue
            ex, ey = e["pos"]
            d = math.hypot(self.pos[0] - ex, self.pos[1] - ey)
            if d < 90 and self.visible(self.pos, e["pos"]) and self.t > 20:
                ux, uy = (self.pos[0] - ex) / max(d, 1e-6), (self.pos[1] - ey) / max(d, 1e-6)
                tgt = (-uy * 6, ux * 6) if d < 40 else (ux * 8, uy * 8)
                e["cool"] -= dt
                if e["cool"] <= 0:
                    e["cool"] = 0.24
                    if self.rng.random() < 0.18:
                        self._hurt(45.0, "gun", math.atan2(ey - self.pos[1], ex - self.pos[0]), e["id"])
                        if self.rng.random() < 0.5:
                            self._hurt(45.0, "gun", math.atan2(ey - self.pos[1], ex - self.pos[0]), e["id"])
            else:
                if e["wp"] is None or math.hypot(e["wp"][0] - ex, e["wp"][1] - ey) < 5:
                    cur, _ = self.zone.at(self.t)
                    aa, rr = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(0, max(10.0, cur[2] - 20))
                    e["wp"] = (cur[0] + rr * math.cos(aa), cur[1] + rr * math.sin(aa))
                ux, uy = e["wp"][0] - ex, e["wp"][1] - ey
                n = max(math.hypot(ux, uy), 1e-6)
                tgt = (ux / n * 8, uy / n * 8)
            nx2, ny2 = ex + tgt[0] * dt, ey + tgt[1] * dt
            if not self.nav_solid(nx2, ny2):
                e["pos"] = [nx2, ny2]
                e["vel"] = [tgt[0], tgt[1]]
            else:
                e["wp"] = None
        # chaos: periodic mortar shells aimed near the bot
        if self.chaos and 30 < self.t < 400 and int(self.t * 10) % 450 == 0 and not self.projectiles:
            sx, sy = self.pos[0] + 60, self.pos[1] + 20
            tx, ty = self.pos[0] + 2, self.pos[1] - 1
            T = 2.2
            vx, vy = (tx - sx) / T, (ty - sy) / T
            z0 = self.ground(sx, sy) + 40
            vz = (self.ground(tx, ty) + 1.0 - z0 + 0.5 * 22 * T * T) / T
            self.projectiles.append({"id": 280, "kind": "mortar", "pos": [sx, sy, z0], "vel": [vx, vy, vz], "_t": 0.0})
        for p in list(self.projectiles):
            p["_t"] += dt
            p["vel"][2] -= 22 * dt
            for k in range(3):
                p["pos"][k] += p["vel"][k] * dt
            if p["pos"][2] <= self.ground(p["pos"][0], p["pos"][1]):
                d = math.hypot(p["pos"][0] - self.pos[0], p["pos"][1] - self.pos[1])
                if d < 7:
                    self._hurt(200 + 160 * (1 - d / 7 * 0.7), "mortar", math.atan2(p["pos"][1] - self.pos[1], p["pos"][0] - self.pos[0]), 9)
                self.projectiles.remove(p)
        # zone + regen
        cur, _ = self.zone.at(self.t)
        outside = math.hypot(self.pos[0] - cur[0], self.pos[1] - cur[1]) > cur[2]
        _, zd = self.zone.at(self.t)
        self.min_margin = min(self.min_margin, cur[2] - math.hypot(self.pos[0] - cur[0], self.pos[1] - cur[1]))
        if outside:
            dmg = zd["dps"] * dt
            self.hp -= dmg
            self.zone_damage += dmg
            self.last_dmg_t = self.t
            self.events.append({"type": "damaged", "amount": dmg, "weapon": "zone", "bearing": None, "by": None})
        elif self.t - self.last_dmg_t > 5.0:
            self.hp = min(self.max_hp, self.hp + 45 * dt)
        if self.hp <= 0:
            self.dead = True
        self.t += dt
        self.tick += 2

    def _hurt(self, amount, weapon, bearing, by):
        self.hp -= amount
        self.enemy_damage += amount
        self.hits_taken += 1
        self.last_dmg_t = self.t
        self.events.append({"type": "damaged", "amount": amount, "weapon": weapon, "bearing": bearing, "by": by})


class SyncGhost(ghost_bot.Ghost):
    """Deterministic variant for the (much faster than real time) simulator: the planner runs inline and its
    result is delivered LAT decisions later, modelling the worker thread's latency in game time."""
    LAT = 3

    def __init__(self, hello):
        self._pending = []
        super().__init__(hello)
        while self.nav is None:
            time.sleep(0.01)

    def _boot(self):
        self.nav = NavMap(self.mapdir)

    def _request(self, req, keep=False):
        self.ver += 1
        req["ver"] = self.ver
        self.req_t = self._t
        self._req = req
        if not keep:
            self.plan = None
        try:
            res = self._compute(req)
        except Exception:
            traceback.print_exc()
            res = None
        self._pending.append([self.LAT, res])

    def act(self, obs):
        for p in self._pending:
            p[0] -= 1
        done = [p for p in self._pending if p[0] <= 0]
        self._pending = [p for p in self._pending if p[0] > 0]
        for p in done:
            if p[1] is not None:
                self.plan_new = p[1]
        return super().act(obs)


def run_match(map_dir, seed=1, seconds=480, chaos=False, enemies=0, verbose=True, spawn=None, threads=False):
    cfg = load_config()
    nav = NavMap(map_dir)
    sim = Sim(nav, cfg, seed, chaos, enemies, spawn)
    hello = {"type": "hello", "protocol": "mechfight/1", "slot": 3, "seed": 12345,
             "map": {"dir": map_dir, "files": ["v2/world.json", "v2/terrain.i16"]}, "config": cfg}
    mf.draw.caps = cfg["debug"]
    bot = (ghost_bot.Ghost if threads else SyncGhost)(hello)
    times, errs = [], []
    steps = int(seconds / 0.1)
    t_ready = None
    for _ in range(steps):
        o = sim.obs()
        mf.draw.clear()
        t0 = time.perf_counter()
        try:
            a = bot.act(json.loads(json.dumps(o)))
            json.dumps(a)            # must be serializable (no NaN checks: check below)
            assert all(math.isfinite(v) for v in a["move"] + a["aim"]), "non-finite action"
            assert len(a["fire"]) == 2
        except Exception:
            errs.append(traceback.format_exc())
            a = {"tick": o["tick"]}
        times.append((time.perf_counter() - t0) * 1000)
        if t_ready is None and bot.nav is not None:
            t_ready = sim.t
        sim.step(a)
        # simulate real-time pacing lightly so the worker thread can progress
        if threads:
            time.sleep(0.02 if len(times) < 60 else 0.001)
        if sim.dead:
            break
    res = {"seed": seed, "survived_s": round(sim.t, 1), "dead": sim.dead, "hp": round(sim.hp), "zone_damage": round(sim.zone_damage), "noisy_s": round(sim.noisy_s, 1),
           "enemy_damage": round(sim.enemy_damage), "min_margin": round(sim.min_margin, 1), "exceptions": len(errs),
           "act_ms_p50": round(statistics.median(times), 2), "act_ms_p99": round(sorted(times)[int(0.99 * len(times)) - 1], 2),
           "act_ms_max": round(max(times), 2), "nav_ready_at_sim_t": t_ready, "fired": sim.fired, "learned_blocks": len(nav.learned),
           "enemies_alive": sum(1 for e in sim.enemies if e["hp"] > 0), "mode_final": bot.mode, "upgrades": sim.upgrades,
           "cubes": sim.cubes_owned, "weapon1": (sim.weapons[1] or {}).get("kind")}
    if verbose:
        print(json.dumps(res))
        for e in errs[:3]:
            print(e)
    return res, errs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="/tmp/arena")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=480)
    ap.add_argument("--chaos", action="store_true")
    ap.add_argument("--enemies", type=int, default=0)
    ap.add_argument("--threads", action="store_true", help="use the real worker thread (planner latency then depends on wall clock)")
    a = ap.parse_args()
    run_match(a.map, a.seed, a.seconds, a.chaos, a.enemies, threads=a.threads)
