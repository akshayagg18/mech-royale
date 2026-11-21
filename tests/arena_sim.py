#!/usr/bin/env python3
"""arena_sim.py -- 16-mech "squad hunt" simulator used to compare Ghost versions.

It is an APPROXIMATION of the practice format (our bot + 15 house bots that share vision, never fight each
other and hunt us weakest-first), built from the official rules (protocol.md) and the real arena map.
It models: 20 Hz physics / 10 Hz decisions, torso turn rate, gun / laser / lightning with the numbers from the
hello config, projectile flight, buildings blocking shots and sight (real map), rear armor, cubes (HP, damage,
size), regeneration, the shrinking zone, 12 NPC drones, 36 crates, 40 floor weapons, supply drops, upgrade
offers, vision (45 m omni / 200 m wedge) and a squad AI with four skill tiers.
It does NOT model: the real house-bot code (unknown), mortar fire, sound, rooftops, tree cover.
Use it to find bugs, to compare versions A/B on identical seeds, and to look at telemetry -- never as a score.

    python3 tests/arena_sim.py --map /tmp/kit/mechfight-kit/arena --bot ghost_bot_v1 --seeds 1-8 --know omni
"""
import argparse
import importlib
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, ".."))                      # flat repo layout (bot modules at the root)
sys.path.insert(0, os.path.join(HERE, "..", "baseline"))        # frozen ghost-v1 (repo layout)
sys.path.insert(0, HERE)

import mechfight as mf  # noqa: E402
from nav import NavMap  # noqa: E402
from sim import Zone, load_config  # noqa: E402

TAU = 2 * math.pi
DT = 0.05                    # physics step (20 Hz); a decision lasts 2 steps
SKILLS = {                   # aim error (deg, 1 sigma), reaction (s), preferred range (m), P(has a 2nd weapon), speed
    "easy": dict(sig=4.5, react=0.60, pref=55, p2=0.00, spd=0.75),
    "medium": dict(sig=2.8, react=0.40, pref=48, p2=0.35, spd=0.85),
    "hard": dict(sig=1.7, react=0.28, pref=45, p2=0.70, spd=0.92),
    "expert": dict(sig=0.9, react=0.16, pref=42, p2=1.00, spd=1.00),
}
ORDER = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
UPG_IDS = ["plating", "armor", "servos", "capacitor", "reactor", "jets", "nanites", "gyros", "sensors",
           "gun_damage", "gun_rate", "gun_velocity", "gun_mag", "laser_charge", "laser_damage", "laser_pierce",
           "lightning_chain", "lightning_range", "lightning_efficiency", "mortar_splash"]


def angd(a, b):
    return (a - b + math.pi) % TAU - math.pi


def n2(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-9 else (0.0, 0.0)


class Mech:
    def __init__(self, mid, x, y, hp, hum=False, skill=None, w2=None):
        self.id, self.x, self.y = mid, x, y
        self.vx = self.vy = 0.0
        self.yaw = self.pitch = 0.0
        self.hp = self.maxhp0 = float(hp)
        self.maxhp = float(hp)
        self.cubes = 0
        self.energy = 100.0
        self.ammo, self.reload, self.gcd = 12, 0.0, 0.0
        self.w2 = w2                  # None or "laser"/"lightning"
        self.w2cd = 0.0
        self.charge = 0.0             # laser charge seconds (0 = not charging)
        self.equip = 0.0
        self.alive = True
        self.kills = 0
        self.last_dmg = -99.0
        self.hum = hum                # our bot
        self.skill = skill
        self.upg = {}
        self.dmg_mult = 1.0
        self.armor = 0.0
        self.regen = 45.0
        self.regen_delay = 5.0
        self.fire_cd = 0.24
        self.turn = 4.0
        self.events = []
        self.offer = None
        self.offer_until = 0.0
        self.next_offer = 1
        self.dead_t = None
        self.place = None
        # telemetry (our mech)
        self.t_dealt = self.t_taken = 0.0
        self.shots = self.hits = 0
        self.zone_dmg = 0.0
        self.t_out = 0.0
        self.npc_kills = self.crate_kills = 0
        self.stuck_s = 0.0
        # hunter AI state
        self.sign = 1
        self.sign_until = 0.0
        self.noise = (0.0, 0.0)
        self.noise_until = 0.0
        self.first_seen = None
        self.laser_hold = 0.0

    @property
    def size(self):
        return 1.0 + 0.05 * self.cubes

    @property
    def radius(self):
        return 2.5 * self.size

    def wpn_name(self):
        return ["gun"] + ([self.w2] if self.w2 else [])


class Arena:
    def __init__(self, nav, cfg, seed, know="omni", ours_cls=None, hunters=15, verbose=False, diff=1.0):
        self.nav, self.cfg = nav, cfg
        self.diff = diff
        self.rng = random.Random(seed * 7919 + 13)
        self.know = know
        self.zone = Zone(cfg, self.rng)
        self.t = 0.0
        self.tick = 0
        self.proj = []
        self.pid = 100
        self.items_id = 5000
        z0 = self.zone.circles[0]
        self.z0 = z0
        self.mechs = []
        self.free_cache = []
        # ---- spawns: 16 spread points on free ground
        pts = []
        tries = 0
        while len(pts) < 16 and tries < 5000:
            tries += 1
            a, r = self.rng.uniform(0, TAU), self.rng.uniform(0.25, 0.85) * z0[2]
            x, y = z0[0] + r * math.cos(a), z0[1] + r * math.sin(a)
            if not self._ok_spot(x, y):
                continue
            if all(math.hypot(x - p[0], y - p[1]) > 60 for p in pts):
                pts.append((x, y))
        slot = self.rng.randrange(16)
        order = list(range(16))
        self.ours = Mech(slot, pts[slot][0], pts[slot][1], cfg["mech"]["hp"], hum=True)
        self.mechs.append(self.ours)
        hs = []
        for k in range(15):
            sk = ORDER[k]
            w2 = None
            if self.rng.random() < SKILLS[sk]["p2"]:
                w2 = self.rng.choice(["laser", "lightning"])
            idx = [i for i in order if i != slot][k]
            h = Mech(idx, pts[idx][0], pts[idx][1], cfg["mech"]["hp"], skill=sk, w2=w2)
            hs.append(h)
        self.hunters = hs
        self.mechs += hs
        self.alive_n = 16
        # ---- loot / npcs / crates
        self.crates = [{"id": 1000 + k, "pos": self._free_pt(z0, 0.9), "hp": 300.0} for k in range(36)]
        self.npcs = []
        for k in range(12):
            hx, hy = self._free_pt(z0, 0.8)
            self.npcs.append({"id": 100 + k, "home": (hx, hy), "x": hx, "y": hy, "vx": 0.0, "vy": 0.0, "hp": 350.0,
                              "cd": self.rng.uniform(0, 1), "wp": None, "agro": None, "agro_t": -9})
        self.pickups = [{"id": 8000 + k, "kind": self.rng.choice(["mortar", "laser", "lightning"]), "pos": self._free_pt(z0, 0.9)}
                        for k in range(40)]
        self.cubes_on = []
        self.drops = []
        self.drop_sched = [(70, 0), (140, 1), (210, 2)]
        self.field = None
        self.field_t = -9.0
        self.field_cell = None
        self.known = None            # squad's knowledge of our position (x, y, t)
        self.verbose = verbose
        self.kills_by_us = 0
        self.deaths = []
        self.trace = []
        self.mode_time = {}

    # ------------------------------------------------------------------ geometry helpers
    def _ok_spot(self, x, y):
        n = self.nav
        if not (-480 < x < 520 and -300 < y < 360):
            return False
        if n.block_at(x, y) or self.solid(x, y):
            return False
        return n.nearest_free(*n.cell_of(x, y), maxr=1) is not None

    def solid(self, x, y):
        n = self.nav
        i, j = int((x - n.ox) / 2.0), int((y - n.oy) / 2.0)
        if 0 <= i < n.fnx and 0 <= j < n.fny:
            return bool(n.solid[j * n.fnx + i])
        return True

    def collide(self, x, y, r=2.0):
        for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            if self.solid(x + dx, y + dy):
                return True
        return False

    def _free_pt(self, z0, frac):
        for _ in range(500):
            a, r = self.rng.uniform(0, TAU), math.sqrt(self.rng.random()) * frac * z0[2]
            x, y = z0[0] + r * math.cos(a), z0[1] + r * math.sin(a)
            if self._ok_spot(x, y) and not self.collide(x, y, 3.0):
                return (x, y)
        return (z0[0], z0[1])

    def gz(self, x, y):
        return self.nav.gh(x, y)

    def los(self, x0, y0, x1, y1):
        return self.nav.los(x0, y0, x1, y1)

    def sees(self, m, x, y):
        dx, dy = x - m.x, y - m.y
        d = math.hypot(dx, dy)
        if d > 200:
            return False
        if d > 45 and abs(angd(math.atan2(dy, dx), m.yaw)) > math.radians(60):
            return False
        return self.los(m.x, m.y, x, y)

    # ------------------------------------------------------------------ damage
    def hurt(self, tgt, amt, src, weapon, bx, by):
        """Damage `tgt` (Mech); src is a Mech or None (zone/npc). bx,by = shooter position for rear/bearing."""
        if not tgt.alive:
            return
        rear = False
        if weapon != "zone":
            bearing = math.atan2(by - tgt.y, bx - tgt.x)
            rear = abs(angd(bearing, tgt.yaw)) > math.pi / 2
            if rear:
                amt *= 1.5
            amt *= max(0.0, 1.0 - tgt.armor)
        tgt.hp -= amt
        tgt.last_dmg = self.t
        if tgt.hum:
            tgt.t_taken += amt
            tgt.events.append({"type": "damaged", "amount": round(amt, 1), "weapon": weapon,
                               "bearing": None if weapon == "zone" else math.atan2(by - tgt.y, bx - tgt.x),
                               "by": None if src is None else src.id, "rear": rear})
        if src is not None and src.hum and weapon != "zone":
            src.t_dealt += amt
            src.hits += 1 if weapon == "gun" else 0
            src.last_dmg = self.t
            src.events.append({"type": "hit", "target": tgt.id, "amount": round(amt, 1), "weapon": weapon})
        elif src is not None:
            src.last_dmg = self.t
        if tgt.hp <= 0:
            self.kill(tgt, src, weapon)

    def kill(self, m, by, cause="zone"):
        if not m.alive:
            return
        self.deaths.append((round(self.t, 1), m.id, None if by is None else by.id, cause))
        m.alive = False
        m.hp = 0
        m.dead_t = self.t
        self.alive_n -= 1
        if by is not None:
            by.kills += 1
            if by.hum:
                self.kills_by_us += 1
                by.events.append({"type": "kill", "target": m.id})
        # drops: every cube plus two; weapons except the gun
        for _ in range(m.cubes + 2):
            self.cubes_on.append({"id": self._iid(), "pos": (m.x + self.rng.uniform(-3, 3), m.y + self.rng.uniform(-3, 3))})
        if m.w2:
            self.pickups.append({"id": self._iid(), "kind": m.w2, "pos": (m.x, m.y)})
            m.w2 = None

    def _iid(self):
        self.items_id += 1
        return self.items_id

    def add_cubes(self, m, n):
        for _ in range(n):
            m.cubes += 1
            frac = 0.04 * min(m.cubes, 10)
            newmax = m.maxhp0 * (1 + frac) * (1.15 ** m.upg.get("plating", 0))
            m.hp += max(0.0, newmax - m.maxhp)
            m.maxhp = newmax
            m.dmg_mult = 1 + frac
            if m.hum:
                m.events.append({"type": "pickup", "cubes": m.cubes})
                if m.cubes >= m.next_offer and m.offer is None:
                    choices = self.rng.sample([u for u in UPG_IDS if m.upg.get(u, 0) < 3], 3)
                    m.offer = {"id": m.cubes, "expires": self.t + 5, "choices": [{"id": c, "name": "", "desc": ""} for c in choices]}
                    m.offer_until = self.t + 5.0
                    for th in (1, 3, 6, 10, 15, 21, 28, 36):
                        if th > m.next_offer:
                            m.next_offer = th
                            break
                    else:
                        m.next_offer = 99

    def apply_upgrade(self, m, uid):
        m.upg[uid] = m.upg.get(uid, 0) + 1
        if uid == "plating":
            old = m.maxhp
            m.maxhp = m.maxhp0 * (1 + 0.04 * min(m.cubes, 10)) * (1.15 ** m.upg["plating"])
            m.hp += m.maxhp - old
        elif uid == "armor":
            m.armor += 0.08
        elif uid == "gun_damage":
            pass
        elif uid == "gun_rate":
            m.fire_cd *= 0.87
        elif uid == "nanites":
            m.regen *= 1.6
            m.regen_delay -= 1.5
        elif uid == "gyros":
            m.turn *= 1.3
        m.events.append({"type": "upgrade", "id": uid})

    # ------------------------------------------------------------------ observation for our bot
    def obs_for(self, m):
        t = self.t
        cur, zd = self.zone.at(t)
        z = self.gz(m.x, m.y)
        inz = math.hypot(m.x - cur[0], m.y - cur[1]) <= cur[2]
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees(m, o.x, o.y):
                mechs.append({"id": o.id, "kind": "mech", "pos": [o.x, o.y, self.gz(o.x, o.y)], "vel": [o.vx, o.vy, 0.0],
                              "aim": [o.yaw, o.pitch], "hp": o.hp, "max_hp": o.maxhp, "cubes": o.cubes,
                              "weapons": o.wpn_name(), "charging": o.charge > 0})
        for n in self.npcs:
            if n["hp"] > 0 and self.sees(m, n["x"], n["y"]):
                mechs.append({"id": n["id"], "kind": "npc", "pos": [n["x"], n["y"], self.gz(n["x"], n["y"])],
                              "vel": [n["vx"], n["vy"], 0.0], "aim": [0.0, 0.0], "hp": n["hp"], "max_hp": 350.0})
        crates = [{"id": c["id"], "pos": [c["pos"][0], c["pos"][1], self.gz(*c["pos"])], "hp": c["hp"]}
                  for c in self.crates if c["hp"] > 0 and math.hypot(c["pos"][0] - m.x, c["pos"][1] - m.y) < 200 and self.sees(m, *c["pos"])]
        cubes = [{"id": c["id"], "pos": [c["pos"][0], c["pos"][1], self.gz(*c["pos"])]} for c in self.cubes_on
                 if math.hypot(c["pos"][0] - m.x, c["pos"][1] - m.y) < 200 and self.sees(m, *c["pos"])]
        pick = [{"id": p["id"], "kind": p["kind"], "pos": [p["pos"][0], p["pos"][1], self.gz(*p["pos"])]} for p in self.pickups
                if math.hypot(p["pos"][0] - m.x, p["pos"][1] - m.y) < 200 and self.sees(m, *p["pos"])]
        projs = []
        for p in self.proj:
            if p["owner"] is m:
                continue
            if math.hypot(p["x"] - m.x, p["y"] - m.y) < 120:
                projs.append({"id": p["id"], "kind": p["kind"], "pos": [p["x"], p["y"], p["z"]], "vel": [p["vx"], p["vy"], p["vz"]]})
        drops = []
        for d in self.drops:
            if d["taken"]:
                continue
            drops.append({"id": d["id"], "pos": [d["pos"][0], d["pos"][1], self.gz(*d["pos"])], "radius": 8,
                          "lands_in": max(0.0, d["land"] - t), "capture_s": 4, "progress": d["prog"], "holder": d["holder"]})
        w0 = {"kind": "gun", "cooldown": m.gcd, "ammo": m.ammo, "reload": m.reload,
              "stats": {"damage": 45 * m.dmg_mult, "cooldown": m.fire_cd, "speed": 252, "range": 230, "spread_deg": 0.6, "mag": 12, "reload": 2.4}}
        w1 = None
        if m.w2:
            st = dict(self.cfg["weapons"][m.w2])
            w1 = {"kind": m.w2, "cooldown": max(m.w2cd, m.equip), "stats": st}
            if m.w2 == "laser":
                w1["charge"] = m.charge
        events, m.events = m.events, []
        o = {"type": "obs", "tick": self.tick, "t": round(t, 2), "alive": self.alive_n,
             "self": {"id": m.id, "pos": [m.x, m.y, z], "vel": [m.vx, m.vy, 0.0], "aim": [m.yaw, m.pitch], "hp": m.hp,
                      "max_hp": m.maxhp, "energy": m.energy, "max_energy": 100, "grounded": True, "cubes": m.cubes,
                      "upgrades": [{"id": k, "n": v} for k, v in m.upg.items()], "weapons": [w0, w1], "in_zone": inz,
                      "stats": {"walk_speed": 11.7 * (1.08 ** m.upg.get("servos", 0)), "boost_speed": 19.5, "turn_rate": m.turn,
                                "muzzle_height": 7, "eye_height": 8}},
             "mechs": mechs, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": pick, "sounds": [], "beams": [],
             "events": events, "zone": zd, "offer": m.offer, "drops": drops}
        return o

    # ------------------------------------------------------------------ physics
    def move(self, m, a, dt):
        ty, tp = a.get("aim", [m.yaw, m.pitch])
        m.yaw += max(-m.turn * dt, min(m.turn * dt, angd(ty, m.yaw)))
        m.pitch += max(-3 * dt, min(3 * dt, tp - m.pitch))
        mv = a.get("move", [0, 0])
        mag = min(1.0, math.hypot(mv[0], mv[1]))
        boost = bool(a.get("boost")) and m.energy > 0 and mag > 0 and m.charge <= 0
        sp = (19.5 if boost else 11.7 * (1.08 ** m.upg.get("servos", 0)))
        if m.charge > 0:
            sp *= 0.5
        wx, wy = (mv[0] / mag * mag * sp, mv[1] / mag * mag * sp) if mag > 1e-9 else (0.0, 0.0)
        if boost:
            m.energy = max(0.0, m.energy - 17 * dt)
        else:
            m.energy = min(100.0, m.energy + 18 * dt)
        for k, w in ((0, wx), (1, wy)):
            v = m.vx if k == 0 else m.vy
            d = w - v
            v += max(-58.5 * dt, min(58.5 * dt, d))
            if k == 0:
                m.vx = v
            else:
                m.vy = v
        nx_, ny_ = m.x + m.vx * dt, m.y + m.vy * dt
        if not self.collide(nx_, ny_):
            m.x, m.y = nx_, ny_
        elif not self.collide(nx_, m.y):
            m.x = nx_
            m.vy = 0.0
        elif not self.collide(m.x, ny_):
            m.y = ny_
            m.vx = 0.0
        else:
            m.vx = m.vy = 0.0
            if m.hum:
                m.stuck_s += dt

    def shoot(self, m, a, dt):
        """Gun + second weapon for mech m."""
        fire = a.get("fire", [False, False])
        m.gcd = max(0.0, m.gcd - dt)
        m.w2cd = max(0.0, m.w2cd - dt)
        m.equip = max(0.0, m.equip - dt)
        if m.reload > 0:
            m.reload = max(0.0, m.reload - dt)
            if m.reload == 0:
                m.ammo = 12
        elif a.get("reload") and m.ammo < 12:
            m.reload = 2.4
        if fire[0] and m.ammo > 0 and m.reload == 0 and m.gcd <= 0 and m.equip <= 0:
            m.gcd += m.fire_cd
            m.ammo -= 1
            spread = math.radians(0.6)
            yaw = m.yaw + self.rng.gauss(0, spread)
            pit = m.pitch + self.rng.gauss(0, spread)
            sp = 252.0 * (1.25 ** m.upg.get("gun_velocity", 0))
            c = math.cos(pit)
            mx, my, mz = m.x + math.cos(yaw) * 3 * c, m.y + math.sin(yaw) * 3 * c, self.gz(m.x, m.y) + 7 + math.sin(pit) * 3
            self.pid += 1
            dmg = 45 * m.dmg_mult * (1.15 ** m.upg.get("gun_damage", 0))
            self.proj.append({"id": self.pid, "kind": "gun", "x": mx, "y": my, "z": mz, "vx": sp * math.cos(yaw) * c,
                              "vy": sp * math.sin(yaw) * c, "vz": sp * math.sin(pit), "owner": m, "dmg": dmg, "life": 230.0 / sp})
            if m.hum:
                m.shots += 1
            if m.ammo == 0:
                m.reload = 2.4
        if m.w2 == "lightning":
            if fire[1] and m.w2cd <= 0 and m.equip <= 0 and m.energy >= 3:
                m.w2cd = 0.25
                m.energy -= 3
                self.lightning(m)
        elif m.w2 == "laser":
            if fire[1] and m.w2cd <= 0 and m.equip <= 0:
                m.charge += dt
                if m.charge >= 0.75 + 2.0:
                    self.laser(m)
            elif m.charge > 0:
                if m.charge >= 0.15 or True:
                    self.laser(m)

    def lightning(self, m):
        tx, ty = None, None
        best = None
        cands = [o for o in self.mechs if o is not m and o.alive]
        for o in cands:
            d = math.hypot(o.x - m.x, o.y - m.y)
            if d < 45 and abs(angd(math.atan2(o.y - m.y, o.x - m.x), m.yaw)) < math.radians(15) and self.los(m.x, m.y, o.x, o.y):
                if best is None or d < best[0]:
                    best = (d, "m", o)
        for n in self.npcs:
            if n["hp"] > 0:
                d = math.hypot(n["x"] - m.x, n["y"] - m.y)
                if d < 45 and abs(angd(math.atan2(n["y"] - m.y, n["x"] - m.x), m.yaw)) < math.radians(15) and self.los(m.x, m.y, n["x"], n["y"]):
                    if best is None or d < best[0]:
                        best = (d, "n", n)
        for c in self.crates:
            if c["hp"] > 0:
                d = math.hypot(c["pos"][0] - m.x, c["pos"][1] - m.y)
                if d < 45 and abs(angd(math.atan2(c["pos"][1] - m.y, c["pos"][0] - m.x), m.yaw)) < math.radians(15) and self.los(m.x, m.y, *c["pos"]):
                    if best is None or d < best[0]:
                        best = (d, "c", c)
        if best is None:
            return
        dmg = 60 * m.dmg_mult
        self.dmg_any(m, best[1], best[2], dmg, "lightning")
        if m.hum:
            m.hits += 0

    def dmg_any(self, src, kind, o, dmg, weapon):
        if kind == "m":
            self.hurt(o, dmg, src, weapon, src.x, src.y)
        elif kind == "n":
            o["hp"] -= dmg
            o["agro"] = src
            o["agro_t"] = self.t
            if src.hum:
                src.t_dealt += dmg
                src.events.append({"type": "hit", "target": o["id"], "amount": dmg, "weapon": weapon})
            if o["hp"] <= 0:
                o["hp"] = 0
                for _ in range(2):
                    self.cubes_on.append({"id": self._iid(), "pos": (o["x"] + self.rng.uniform(-2, 2), o["y"] + self.rng.uniform(-2, 2))})
                if src.hum:
                    src.npc_kills += 1
        else:
            o["hp"] -= dmg
            if src.hum:
                src.t_dealt += dmg
                src.events.append({"type": "hit", "target": o["id"], "amount": dmg, "weapon": weapon})
            if o["hp"] <= 0:
                o["hp"] = 0
                self.cubes_on.append({"id": self._iid(), "pos": (o["pos"][0], o["pos"][1])})
                if self.rng.random() < 0.5:
                    self.pickups.append({"id": self._iid(), "kind": self.rng.choice(["mortar", "laser", "lightning"]), "pos": o["pos"]})
                if src.hum:
                    src.crate_kills += 1

    def laser(self, m):
        full = 0.75
        frac = min(1.0, m.charge / full)
        dmg = 315 * (0.35 + 0.65 * frac) * m.dmg_mult
        m.charge = 0.0
        m.w2cd = 1.65
        dx, dy = math.cos(m.yaw) * math.cos(m.pitch), math.sin(m.yaw) * math.cos(m.pitch)
        best = None
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            rx, ry = o.x - m.x, o.y - m.y
            al = rx * dx + ry * dy
            if al < 0 or al > 280:
                continue
            if abs(dx * ry - dy * rx) < o.radius * 1.1 and self.los(m.x, m.y, o.x, o.y):
                if best is None or al < best[0]:
                    best = (al, "m", o)
        for n in self.npcs:
            if n["hp"] <= 0:
                continue
            rx, ry = n["x"] - m.x, n["y"] - m.y
            al = rx * dx + ry * dy
            if 0 < al < 280 and abs(dx * ry - dy * rx) < 2.5 and self.los(m.x, m.y, n["x"], n["y"]):
                if best is None or al < best[0]:
                    best = (al, "n", n)
        for c in self.crates:
            if c["hp"] <= 0:
                continue
            rx, ry = c["pos"][0] - m.x, c["pos"][1] - m.y
            al = rx * dx + ry * dy
            if 0 < al < 280 and abs(dx * ry - dy * rx) < 3.0 and self.los(m.x, m.y, *c["pos"]):
                if best is None or al < best[0]:
                    best = (al, "c", c)
        if best:
            self.dmg_any(m, best[1], best[2], dmg, "laser")

    def step_projectiles(self, dt):
        keep = []
        for p in self.proj:
            ox, oy, oz = p["x"], p["y"], p["z"]
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            p["z"] += p["vz"] * dt
            p["life"] -= dt
            if p["life"] <= 0 or p["z"] < self.gz(p["x"], p["y"]) or not self.los(ox, oy, p["x"], p["y"]):
                continue
            hit = False
            sx, sy = p["x"] - ox, p["y"] - oy
            L2 = sx * sx + sy * sy
            for o in self.mechs:
                if o is p["owner"] or not o.alive or (not o.hum and not p["owner"].hum):
                    continue
                u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((o.x - ox) * sx + (o.y - oy) * sy) / L2))
                d = math.hypot(o.x - (ox + sx * u), o.y - (oy + sy * u))
                zz = oz + (p["z"] - oz) * u
                gzo = self.gz(o.x, o.y)
                if d < o.radius and gzo - 0.5 <= zz <= gzo + 9.0 * o.size:
                    self.hurt(o, p["dmg"], p["owner"], p["kind"], ox, oy)
                    hit = True
                    break
            if not hit and p["owner"].hum or (not hit and p["kind"] == "gun" and p["owner"] is not None and not isinstance(p["owner"], dict)):
                for n in self.npcs:
                    if n["hp"] > 0 and math.hypot(n["x"] - p["x"], n["y"] - p["y"]) < 3.0 and abs(p["z"] - self.gz(n["x"], n["y"]) - 1.8) < 3.5:
                        if isinstance(p["owner"], Mech):
                            self.dmg_any(p["owner"], "n", n, p["dmg"], "gun")
                        hit = True
                        break
                if not hit and isinstance(p["owner"], Mech):
                    for c in self.crates:
                        if c["hp"] > 0 and math.hypot(c["pos"][0] - p["x"], c["pos"][1] - p["y"]) < 3.2 and abs(p["z"] - self.gz(*c["pos"]) - 3) < 4.5:
                            self.dmg_any(p["owner"], "c", c, p["dmg"], "gun")
                            hit = True
                            break
            if not hit:
                keep.append(p)
        self.proj = keep

    # ------------------------------------------------------------------ NPCs, drops, pickups
    def step_world(self, dt):
        t = self.t
        for n in self.npcs:
            if n["hp"] <= 0:
                continue
            tgt = None
            if n["agro"] is not None and n["agro"].alive and t - n["agro_t"] < 8:
                tgt = n["agro"]
            else:
                for m in self.mechs:
                    if m.alive and math.hypot(m.x - n["x"], m.y - n["y"]) < 45 and math.hypot(n["x"] - n["home"][0], n["y"] - n["home"][1]) < 30 \
                            and self.los(n["x"], n["y"], m.x, m.y):
                        tgt = m
                        break
            n["cd"] -= dt
            if tgt is not None:
                dx, dy = tgt.x - n["x"], tgt.y - n["y"]
                d = math.hypot(dx, dy)
                if n["cd"] <= 0 and d < 60 and self.los(n["x"], n["y"], tgt.x, tgt.y):
                    n["cd"] = 1.0
                    if self.rng.random() < 0.55:
                        self.hurt(tgt, 25.0, None, "npc", n["x"], n["y"])
                ux, uy = n2(dx, dy)
                sp = 7.0 if d > 25 else 0.0
                if math.hypot(n["x"] - n["home"][0], n["y"] - n["home"][1]) > 60 or d > 90:
                    n["agro"] = None
                    ux, uy = n2(n["home"][0] - n["x"], n["home"][1] - n["y"])
                    sp = 7.0
            else:
                if n["wp"] is None or math.hypot(n["wp"][0] - n["x"], n["wp"][1] - n["y"]) < 3:
                    a, r = self.rng.uniform(0, TAU), self.rng.uniform(0, 25)
                    n["wp"] = (n["home"][0] + r * math.cos(a), n["home"][1] + r * math.sin(a))
                ux, uy = n2(n["wp"][0] - n["x"], n["wp"][1] - n["y"])
                sp = 3.0
            nx_, ny_ = n["x"] + ux * sp * dt, n["y"] + uy * sp * dt
            if not self.collide(nx_, ny_, 1.5):
                n["x"], n["y"], n["vx"], n["vy"] = nx_, ny_, ux * sp, uy * sp
            else:
                n["wp"] = None
        # supply drops
        for (tt, k) in list(self.drop_sched):
            if t >= tt:
                self.drop_sched.remove((tt, k))
                c = self.zone.circles[min(k + 1, len(self.zone.circles) - 1)]
                pos = self._free_pt(c, 0.6)
                self.drops.append({"id": 20000 + k, "pos": pos, "land": tt + 25.0, "prog": 0.0, "holder": None, "taken": False})
        for d in self.drops:
            if d["taken"] or t < d["land"]:
                continue
            inside = [m for m in self.mechs if m.alive and math.hypot(m.x - d["pos"][0], m.y - d["pos"][1]) <= 8.0]
            if len(inside) == 1:
                if d["holder"] != inside[0].id:
                    d["holder"], d["prog"] = inside[0].id, 0.0
                d["prog"] += dt
                if d["prog"] >= 4.0:
                    m = inside[0]
                    self.add_cubes(m, 3)
                    m.hp, m.energy = m.maxhp, 100.0
                    d["taken"] = True
            else:
                d["holder"], d["prog"] = None, 0.0
        # cubes -> closest mech inside 3.5 m
        for c in list(self.cubes_on):
            best = None
            for m in self.mechs:
                if m.alive:
                    dd = math.hypot(m.x - c["pos"][0], m.y - c["pos"][1])
                    if dd <= 3.5 and (best is None or dd < best[0] or (dd == best[0] and m.id < best[1].id)):
                        best = (dd, m)
            if best:
                self.cubes_on.remove(c)
                self.add_cubes(best[1], 1)

    def take(self, m, pid):
        for p in self.pickups:
            if p["id"] == pid and math.hypot(p["pos"][0] - m.x, p["pos"][1] - m.y) <= 4.0 and m.w2 is None and p["kind"] in ("laser", "lightning"):
                m.w2 = p["kind"]
                m.w2cd = 0.0
                m.equip = 1.65 if p["kind"] == "laser" else 1.0
                self.pickups.remove(p)
                if m.hum:
                    m.events.append({"type": "took", "id": pid, "kind": m.w2, "slot": 1})
                return
        if m.hum:
            m.events.append({"type": "take_failed", "id": pid})

    # ------------------------------------------------------------------ house bots
    def squad_update(self):
        t = self.t
        o = self.ours
        if not o.alive:
            return
        sensed = False
        for h in self.hunters:
            if h.alive and self.sees(h, o.x, o.y):
                sensed = True
                break
        loud = o.gcd > 0.0 and o.gcd > o.fire_cd - 0.15   # fired in the last decision (audible 320 m)
        moving_loud = math.hypot(o.vx, o.vy) > 5.0
        if self.know == "omni":
            self.known = (o.x, o.y, t)
        else:
            if sensed:
                self.known = (o.x, o.y, t)
            elif loud and self.hunters:
                self.known = (o.x + self.rng.uniform(-30, 30), o.y + self.rng.uniform(-30, 30), t)
            elif moving_loud:
                for h in self.hunters:
                    if h.alive and math.hypot(h.x - o.x, h.y - o.y) < 40:
                        self.known = (o.x, o.y, t)
                        break
        # distance field from the squad's target
        if self.known is not None:
            kx, ky, _ = self.known
            cell = self.nav.cell_of(kx, ky)
            if self.field is None or (t - self.field_t > 1.5 and cell != self.field_cell):
                s = self.nav.nearest_free(*cell, maxr=4)
                if s is not None:
                    dist, _ = self.nav.dijkstra(*s)
                    self.field, self.field_t, self.field_cell = dist, t, cell

    def n_active(self):
        t = self.t
        if t < 25:
            return 0
        n = 1 + int((t - 25) * self.diff / 45.0) + self.kills_by_us
        if t > 300:
            n = 15
        return min(15, n)

    def hunter_action(self, h, idx_active):
        o = self.ours
        t = self.t
        a = {"move": [0.0, 0.0], "boost": False, "aim": [h.yaw, h.pitch], "fire": [False, False]}
        sk = dict(SKILLS[h.skill])
        sk["sig"] /= self.diff
        sk["react"] /= self.diff
        cur, zd = self.zone.at(t)
        nxt = (zd["next_center"], zd["next_radius"])
        out = math.hypot(h.x - cur[0], h.y - cur[1]) > cur[2] - 8
        far_next = math.hypot(h.x - nxt[0][0], h.y - nxt[0][1]) > nxt[1] - 10
        if not idx_active or not o.alive or self.known is None:
            # idle: move to a loose point inside the next circle, then stand
            dx, dy = nxt[0][0] - h.x, nxt[0][1] - h.y
            d = math.hypot(dx, dy)
            if far_next and d > 10:
                ux, uy = n2(dx, dy)
                a["move"] = [ux * sk["spd"], uy * sk["spd"]]
                a["aim"] = [math.atan2(uy, ux), 0.0]
            return a
        kx, ky, kt = self.known
        tx, ty = (o.x, o.y) if t - kt < 0.5 or self.know == "omni" else (kx, ky)
        dx, dy = tx - h.x, ty - h.y
        d = math.hypot(dx, dy)
        vis = self.los(h.x, h.y, o.x, o.y) and math.hypot(o.x - h.x, o.y - h.y) < 230
        if vis:
            if h.first_seen is None:
                h.first_seen = t
        else:
            h.first_seen = None
        # aim with lead + noise
        if t > h.noise_until:
            h.noise = (self.rng.gauss(0, math.radians(sk["sig"])), self.rng.gauss(0, math.radians(sk["sig"])))
            h.noise_until = t + 0.35
        if vis:
            dd = math.hypot(o.x - h.x, o.y - h.y)
            tf = dd / 252.0
            px, py = o.x + o.vx * tf, o.y + o.vy * tf
            yaw_t = math.atan2(py - h.y, px - h.x) + h.noise[0]
            hz = self.gz(h.x, h.y) + 7
            oz = self.gz(o.x, o.y) + 4.5 * o.size
            pit_t = math.atan2(oz - hz, math.hypot(px - h.x, py - h.y)) + h.noise[1]
            a["aim"] = [yaw_t, max(-0.7, min(1.3, pit_t))]
            err = abs(angd(yaw_t, h.yaw))
            reacted = h.first_seen is not None and t - h.first_seen >= sk["react"]
            if reacted and dd < 150 and err < math.radians(3.0 + sk["sig"]):
                a["fire"][0] = True
            if h.w2 == "lightning" and dd < 40 and reacted and err < math.radians(10):
                a["fire"][1] = True
            if h.w2 == "laser" and reacted and dd < 170 and h.w2cd <= 0 and h.equip <= 0:
                if h.charge > 0:
                    a["fire"][1] = h.charge < 0.75
                elif err < math.radians(4.0):
                    a["fire"][1] = True
        else:
            a["aim"] = [math.atan2(dy, dx), 0.0]
        # movement
        pref = sk["pref"] - (12 if h.w2 == "lightning" else 0)
        if out:
            ux, uy = n2(cur[0] - h.x, cur[1] - h.y)
            mv = (ux, uy)
        elif vis and d < pref + 8:
            if t > h.sign_until:
                h.sign = self.rng.choice((-1, 1))
                h.sign_until = t + self.rng.uniform(0.7, 1.8)
            ux, uy = n2(dx, dy)
            k = 0.35 if d > pref - 8 else -0.5
            mv = (-uy * h.sign + ux * k, ux * h.sign + uy * k)
        else:
            mv = self.descend(h, dx, dy)
        n = math.hypot(*mv)
        if n > 1e-9:
            a["move"] = [mv[0] / n * sk["spd"], mv[1] / n * sk["spd"]]
        if h.ammo == 0:
            a["reload"] = True
        return a

    def descend(self, h, dx, dy):
        f = self.field
        if f is None:
            return n2(dx, dy)
        nav = self.nav
        i, j = nav.cell_of(h.x, h.y)
        best, bk = None, None
        base = f[j * nav.nx + i]
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if not di and not dj:
                    continue
                a, b = i + di, j + dj
                if 0 <= a < nav.nx and 0 <= b < nav.ny:
                    v = f[b * nav.nx + a]
                    if v < 1e17 and (best is None or v < best):
                        best, bk = v, (a, b)
        if bk is None or (base < 1e17 and best >= base and math.hypot(dx, dy) < 12):
            return n2(dx, dy)
        cx, cy = nav.center_of(*bk)
        return n2(cx - h.x, cy - h.y)

    # ------------------------------------------------------------------ main loop
    def run(self, bot, seconds=480.0):
        o = self.ours
        times = []
        errs = []
        mf.draw.caps = self.cfg["debug"]
        act = {"tick": 0}
        end_reason = "time"
        steps = int(seconds / (2 * DT))
        for _ in range(steps):
            t = self.t
            self.squad_update()
            # our decision
            ob = self.obs_for(o) if o.alive else None
            if o.alive:
                mf.draw.clear()
                t0 = time.perf_counter()
                try:
                    act = bot.act(json.loads(json.dumps(ob)))
                    assert all(math.isfinite(v) for v in act["move"] + act["aim"])
                except Exception:
                    errs.append(traceback.format_exc())
                    act = {"tick": ob["tick"]}
                times.append((time.perf_counter() - t0) * 1000)
                if o.offer is not None:
                    if "pick" in act:
                        self.apply_upgrade(o, o.offer["choices"][int(act["pick"]) % len(o.offer["choices"])]["id"])
                        o.offer = None
                    elif t >= o.offer_until:
                        self.apply_upgrade(o, o.offer["choices"][0]["id"])
                        o.offer = None
            n_act = self.n_active()
            if o.alive and len(times) % 10 == 1:
                near = sum(1 for h in self.hunters if h.alive and math.hypot(h.x - o.x, h.y - o.y) < 100)
                vis_n = sum(1 for h in self.hunters if h.alive and self.sees(o, h.x, h.y))
                self.trace.append((round(self.t, 1), round(o.hp), getattr(bot, "mode", "?"), n_act, near, vis_n, o.w2 or "-", o.cubes))
                self.mode_time[getattr(bot, "mode", "?")] = self.mode_time.get(getattr(bot, "mode", "?"), 0) + 1
            hacts = []
            rank = 0
            for h in self.hunters:
                if not h.alive:
                    hacts.append(None)
                    continue
                active = rank < n_act
                hacts.append(self.hunter_action(h, active))
                rank += 1 if True else 0
            for sub in range(2):
                if o.alive:
                    self.shoot_and_move(o, act, DT, first=(sub == 0))
                for h, ha in zip(self.hunters, hacts):
                    if h.alive and ha is not None:
                        self.shoot_and_move(h, ha, DT, first=(sub == 0))
                self.step_projectiles(DT)
                self.step_world(DT)
                self.zone_regen(DT)
                self.t += DT
                self.tick += 1
            # placement bookkeeping
            if not o.alive and o.place is None:
                o.place = 1 + self.alive_n
                end_reason = "dead"
                break
            if self.alive_n == 1 and o.alive:
                o.place = 1
                end_reason = "won"
                break
        if o.place is None:
            surv = [m for m in self.mechs if m.alive]
            o.place = 1 + sum(1 for m in surv if m is not o and m.hp > o.hp)
        return {"place": o.place, "points": 16 - o.place, "end": end_reason, "t": round(self.t, 1), "kills": o.kills,
                "hp_end": round(o.hp), "dealt": round(o.t_dealt), "taken": round(o.t_taken), "shots": o.shots, "hits": o.hits,
                "acc": round(o.hits / o.shots, 3) if o.shots else 0.0, "npc_kills": o.npc_kills, "crates": o.crate_kills,
                "cubes": o.cubes, "weapon": o.w2, "zone_dmg": round(o.zone_dmg), "t_out": round(o.t_out, 1), "stuck_s": round(o.stuck_s, 1),
                "act_ms_p50": round(statistics.median(times), 2) if times else 0, "act_ms_p99": round(sorted(times)[int(0.99 * len(times)) - 1], 2) if times else 0,
                "act_ms_max": round(max(times), 2) if times else 0, "errors": len(errs), "alive_at_end": self.alive_n,
                "hunters_dead": sum(1 for h in self.hunters if not h.alive), "deaths": ";".join("%s:%s>%s/%s" % d for d in self.deaths), "modes": ",".join("%s=%d" % kv for kv in sorted(self.mode_time.items())), "trace": self.trace[-14:],
                "upg": ",".join(k + str(v) for k, v in o.upg.items())}, errs

    def shoot_and_move(self, m, a, dt, first):
        if first and a.get("take") is not None:
            self.take(m, a["take"])
        self.move(m, a, dt)
        # laser release: no fire flag while charging -> release handled in shoot()
        self.shoot(m, a, dt)

    def zone_regen(self, dt):
        cur, zd = self.zone.at(self.t)
        for m in self.mechs:
            if not m.alive:
                continue
            out = math.hypot(m.x - cur[0], m.y - cur[1]) > cur[2]
            if out:
                dmg = zd["dps"] * dt
                m.last_dmg = self.t
                m.zone_dmg += dmg if m.hum else 0
                if m.hum:
                    m.t_out += dt
                    m.events.append({"type": "damaged", "amount": dmg, "weapon": "zone", "bearing": None, "by": None})
                m.hp -= dmg
                if m.hp <= 0:
                    self.kill(m, None)
            elif self.t - m.last_dmg > m.regen_delay:
                m.hp = min(m.maxhp, m.hp + m.regen * dt)


# ---------------------------------------------------------------------- bot wrapper
def make_sync(base):
    class SyncBot(base):
        LAT = 3

        def __init__(self, hello, nav):
            self._pending = []
            self._nav = nav
            super().__init__(hello)
            while self.nav is None:
                time.sleep(0.01)

        def _boot(self):
            self.nav = self._nav

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
    return SyncBot


_NAV = {}


def run_one(args):
    map_dir, botmod, seed, know, seconds, diff, cfgover = args
    cfg = load_config()
    if map_dir not in _NAV:
        _NAV[map_dir] = NavMap(map_dir)
    nav = _NAV[map_dir]
    nav.learned.clear()
    mod = importlib.import_module(botmod)
    if cfgover and hasattr(mod, "CFG"):
        for kv in cfgover.split(","):
            k, v = kv.split("=")
            mod.CFG[k] = (v.lower() == "true") if v.lower() in ("true", "false") else float(v)
    Bot = make_sync(mod.Ghost)
    arena = Arena(nav, cfg, seed, know, diff=diff)
    hello = {"type": "hello", "protocol": "mechfight/1", "slot": arena.ours.id, "seed": 12345 + seed,
             "map": {"dir": map_dir, "files": ["v2/world.json", "v2/terrain.i16"]}, "config": cfg}
    bot = Bot(hello, nav)
    t0 = time.time()
    res, errs = arena.run(bot, seconds)
    res["seed"] = seed
    res["wall_s"] = round(time.time() - t0, 1)
    if errs:
        res["first_error"] = errs[0][-600:]
    return res


def parse_seeds(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="/tmp/kit/mechfight-kit/arena")
    ap.add_argument("--bot", default="ghost_bot")
    ap.add_argument("--seeds", default="1-6")
    ap.add_argument("--know", default="omni", choices=["omni", "sense"])
    ap.add_argument("--seconds", type=float, default=480)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--cfg", default="", help="override bot CFG, e.g. FARM=false,OPP=false")
    ap.add_argument("--diff", type=float, default=1.0, help="hunter skill/aggression multiplier")
    ap.add_argument("--json", default="")
    ap.add_argument("--trace", action="store_true", help="print the last trace rows of each match")
    a = ap.parse_args()
    jobs = [(a.map, a.bot, s, a.know, a.seconds, a.diff, a.cfg) for s in parse_seeds(a.seeds)]
    if a.procs > 1 and len(jobs) > 1:
        with mp.Pool(a.procs) as pool:
            rows = pool.map(run_one, jobs)
    else:
        rows = [run_one(j) for j in jobs]
    keys = ["seed", "points", "place", "end", "t", "kills", "hp_end", "dealt", "taken", "acc", "npc_kills", "crates", "cubes", "weapon",
            "zone_dmg", "t_out", "stuck_s", "act_ms_p99", "errors", "wall_s", "upg"]
    print("\t".join(keys))
    for r in rows:
        print("\t".join(str(r.get(k, "")) for k in keys))
    pts = [r["points"] for r in rows]
    print(f"\nbot={a.bot} cfg={a.cfg or '-'} know={a.know} diff={a.diff} n={len(rows)} mean_points={statistics.mean(pts):.2f} median={statistics.median(pts)} "
          f"mean_kills={statistics.mean(r['kills'] for r in rows):.2f} mean_t={statistics.mean(r['t'] for r in rows):.0f}s "
          f"errors={sum(r['errors'] for r in rows)}")
    if a.trace:
        for r in rows:
            print("seed", r["seed"], "modes", r.get("modes"), "deaths", r.get("deaths"))
            for row in r.get("trace", []):
                print("   t=%s hp=%s mode=%s active=%s near100=%s vis=%s w2=%s cubes=%s" % row)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f)
