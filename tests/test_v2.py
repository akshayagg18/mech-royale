#!/usr/bin/env python3
"""Focused decision tests for Ghost-v2 (real arena map, synthetic observations).

    python3 tests/test_v2.py [--map /tmp/kit/mechfight-kit/arena]     (stdlib unittest)

These pin the *decisions* (crate / drone / weapon farming, fight selection, retreat threshold, zone push, supply-drop
reach, sound aim, ammo and laser handling, protocol edge cases, stuck recovery, latency) -- not the game score.
"""
import math
import os
import statistics
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, ".."))                      # flat repo layout (bot modules at the root)
sys.path.insert(0, os.path.join(HERE, "..", "baseline"))        # frozen ghost-v1 (repo layout)
sys.path.insert(0, HERE)
MAP = os.environ.get("MECH_MAP", "/tmp/kit/mechfight-kit/arena")
for i, a in enumerate(sys.argv):
    if a == "--map" and i + 1 < len(sys.argv):
        MAP = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        break

import mechfight as mf  # noqa: E402
import ghost_bot  # noqa: E402
from ghost_bot import Ghost, CFG  # noqa: E402
from sim import load_config  # noqa: E402

CFGJ = load_config()
HELLO = {"type": "hello", "protocol": "mechfight/1", "slot": 3, "seed": 5, "map": {"dir": MAP, "files": []}, "config": CFGJ}
GUN = {"kind": "gun", "cooldown": 0.0, "ammo": 12, "reload": 0.0,
       "stats": {"damage": 45, "cooldown": 0.24, "speed": 252, "range": 230, "spread_deg": 0.6, "mag": 12, "reload": 2.4}}
LIGHTNING = {"kind": "lightning", "cooldown": 0.0, "stats": {"damage": 60, "range": 45, "cone_deg": 30, "cooldown": 0.25}}
LASER = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": {"damage": 315, "range": 280, "charge": 0.75}}
ZONE = {"center": [41.3, -72.8], "radius": 560, "next_center": [41.3, -72.8], "next_radius": 380,
        "shrink_start": 60, "shrink_end": 110, "dps": 20}

_bot = {}


def get_bot():
    """A fresh Ghost whose nav is the shared (already loaded) map; planner worker disabled for determinism."""
    from nav import NavMap
    if "nav" not in _bot:
        _bot["nav"] = NavMap(MAP)

    class T(Ghost):
        def _boot(self):
            self.nav = _bot["nav"]

        def _request(self, req, keep=False):
            self.ver += 1
            req["ver"] = self.ver
            self.req_t = self._t
            try:
                res = self._compute(req)
            except Exception:
                res = None
            if res is not None:
                self.plan_new = res
    b = T(HELLO)
    while b.nav is None:
        time.sleep(0.01)
    b.nav.learned.clear()
    return b


def open_point(dist=40.0, tries=4000):
    """A free spot with a clear straight line of `dist` metres to the east, inside the first zone phase."""
    nav = get_bot().nav
    import random
    r = random.Random(4)
    for _ in range(tries):
        x, y = 41.3 + r.uniform(-150, 150), -72.8 + r.uniform(-150, 150)
        if nav.block_at(x, y) or nav.block_at(x + dist, y):
            continue
        if nav.free_ray(x, y, 1.0, 0.0, dist + 4) and nav.los(x, y, x + dist, y) and nav.exposure(x, y)[0] > -1:
            return x, y
    raise RuntimeError("no open point")


def mk_self(x, y, hp=1400.0, weapons=None, aim=(0.0, 0.0), cubes=0, energy=100.0):
    return {"id": 3, "pos": [x, y, get_bot().nav.gh(x, y)], "vel": [0, 0, 0], "aim": list(aim), "hp": hp, "max_hp": 1400.0,
            "energy": energy, "max_energy": 100, "grounded": True, "cubes": cubes, "upgrades": [], "in_zone": True,
            "weapons": weapons or [dict(GUN), None],
            "stats": {"walk_speed": 11.7, "boost_speed": 19.5, "turn_rate": 4, "muzzle_height": 7, "eye_height": 8}}


def mk_obs(t, me, **kw):
    o = {"type": "obs", "tick": int(t * 20), "t": t, "alive": 16, "self": me, "mechs": [], "projectiles": [], "crates": [],
         "cubes": [], "pickups": [], "sounds": [], "beams": [], "events": [], "drops": [], "zone": dict(ZONE), "offer": None}
    o.update(kw)
    return o


def mk_mech(mid, x, y, hp=1400.0, kind="mech", weapons=None, vel=(0, 0), aim=0.0, cubes=0):
    m = {"id": mid, "kind": kind, "pos": [x, y, get_bot().nav.gh(x, y)], "vel": [vel[0], vel[1], 0.0], "aim": [aim, 0.0],
         "hp": hp, "max_hp": 1400.0 if kind == "mech" else 350.0, "cubes": cubes, "weapons": weapons or ["gun"], "charging": False}
    return m


def run_acts(b, t0, me_fn, obs_fn, n=12, dt=0.1):
    """Drive act() for n decisions, feeding the commanded aim back as the actual aim (instant torso)."""
    last = None
    for k in range(n):
        t = t0 + k * dt
        me = me_fn(last)
        o = obs_fn(t, me)
        last = b.act(o)
    return last


class FarmTests(unittest.TestCase):
    def test_crate_in_range_is_shot_and_not_approached_closer_than_stand(self):
        x, y = open_point(30)
        b = get_bot()
        crate = {"id": 1007, "pos": [x + 28, y, b.nav.gh(x + 28, y)], "hp": 300.0}
        aim = [0.0, 0.0]

        def me_fn(last):
            if last is not None:
                aim[0], aim[1] = last["aim"]
            return mk_self(x, y, aim=aim)
        a = run_acts(b, 10.0, me_fn, lambda t, me: mk_obs(t, me, crates=[crate]), n=20)
        self.assertEqual(b.mode, "farm")
        self.assertTrue(a["fire"][0], "should be firing at the crate")
        self.assertLess(math.hypot(a["move"][0], a["move"][1]), 1e-6, "inside firing distance -> hold position")

    def test_far_crate_is_approached(self):
        x, y = open_point(60)
        b = get_bot()
        crate = {"id": 1008, "pos": [x + 55, y, b.nav.gh(x + 55, y)], "hp": 300.0}
        a = run_acts(b, 10.0, lambda last: mk_self(x, y), lambda t, me: mk_obs(t, me, crates=[crate]), n=6)
        self.assertEqual(b.mode, "farm")
        self.assertGreater(a["move"][0], 0.5)

    def test_no_farming_with_enemy_in_view(self):
        x, y = open_point(100)
        b = get_bot()
        crate = {"id": 1009, "pos": [x + 25, y, b.nav.gh(x + 25, y)], "hp": 300.0}
        en = mk_mech(9, x + 85, y)           # in line of sight, inside CALM_R (100 m)
        run_acts(b, 10.0, lambda last: mk_self(x, y), lambda t, me: mk_obs(t, me, crates=[crate], mechs=[en]), n=4)
        self.assertNotEqual(b.mode, "farm")

    def test_no_farming_below_hp_floor(self):
        x, y = open_point(40)
        b = get_bot()
        crate = {"id": 1010, "pos": [x + 25, y, b.nav.gh(x + 25, y)], "hp": 300.0}
        run_acts(b, 10.0, lambda last: mk_self(x, y, hp=500), lambda t, me: mk_obs(t, me, crates=[crate]), n=4)
        self.assertNotEqual(b.mode, "farm")

    def test_crate_outside_next_circle_ignored(self):
        x, y = 41.3 + 370.0, -72.8                  # near the rim of the 380 m circle
        b = get_bot()
        if b.nav.block_at(x, y):
            self.skipTest("rim point blocked")
        crate = {"id": 1011, "pos": [x + 20, y, b.nav.gh(x + 20, y)], "hp": 300.0}
        b.ft = None
        r = b._farm(mk_obs(10.0, mk_self(x, y), crates=[crate]), mk_self(x, y), [], ZONE["next_center"], 380.0, 10.0, x, y)
        self.assertIsNone(r)

    def test_late_game_crates_skipped_but_cubes_taken(self):
        x, y = open_point(40)
        b = get_bot()
        crate = {"id": 1012, "pos": [x + 25, y, b.nav.gh(x + 25, y)], "hp": 300.0}
        cube = {"id": 5100, "pos": [x + 12, y, b.nav.gh(x + 12, y)]}
        me = mk_self(x, y)
        r = b._farm(mk_obs(250.0, me, crates=[crate]), me, [], ZONE["next_center"], 380.0, 250.0, x, y)
        self.assertIsNone(r)
        b.ft = None
        r = b._farm(mk_obs(250.0, me, cubes=[cube]), me, [], ZONE["next_center"], 380.0, 250.0, x, y)
        self.assertIsNotNone(r)
        self.assertGreater(r["mx"], 0.5)

    def test_npc_nearest_preferred_and_shot(self):
        x, y = open_point(40)
        b = get_bot()
        near = mk_mech(100, x + 26, y, kind="npc")
        far = mk_mech(101, x + 70, y + 5, kind="npc")
        aim = [0.0, 0.0]

        def me_fn(last):
            if last is not None:
                aim[0], aim[1] = last["aim"]
            return mk_self(x, y, aim=aim)
        a = run_acts(b, 10.0, me_fn, lambda t, me: mk_obs(t, me, mechs=[near, far]), n=20)
        self.assertEqual(b.mode, "farm")
        self.assertEqual(b.ft["id"], 100)
        self.assertTrue(a["fire"][0])

    def test_lightning_pickup_beats_laser_and_cube(self):
        x, y = open_point(40)
        b = get_bot()
        lg = {"id": 8001, "kind": "lightning", "pos": [x + 20, y + 3, b.nav.gh(x + 20, y + 3)]}
        ls = {"id": 8002, "kind": "laser", "pos": [x + 20, y - 3, b.nav.gh(x + 20, y - 3)]}
        cube = {"id": 5101, "pos": [x + 18, y, b.nav.gh(x + 18, y)]}
        me = mk_self(x, y)
        b.ft = None
        r = b._farm(mk_obs(10.0, me, pickups=[ls, lg], cubes=[cube]), me, [], ZONE["next_center"], 380.0, 10.0, x, y)
        self.assertEqual(b.ft["id"], 8001)

    def test_pickup_ignored_when_slot_full_and_mortar_ignored(self):
        x, y = open_point(40)
        b = get_bot()
        lg = {"id": 8003, "kind": "lightning", "pos": [x + 20, y, b.nav.gh(x + 20, y)]}
        mo = {"id": 8004, "kind": "mortar", "pos": [x + 10, y, b.nav.gh(x + 10, y)]}
        me = mk_self(x, y, weapons=[dict(GUN), dict(LASER)])
        b.ft = None
        self.assertIsNone(b._farm(mk_obs(10.0, me, pickups=[lg, mo]), me, [], ZONE["next_center"], 380.0, 10.0, x, y))
        me = mk_self(x, y)
        b.ft = None
        r = b._farm(mk_obs(10.0, me, pickups=[mo]), me, [], ZONE["next_center"], 380.0, 10.0, x, y)
        self.assertIsNone(r)

    def test_take_action_only_in_reach_and_only_good_weapons(self):
        x, y = open_point(40)
        b = get_bot()
        me = mk_self(x, y)
        lg = {"id": 8005, "kind": "lightning", "pos": [x + 3.0, y, b.nav.gh(x + 3, y)]}
        mo = {"id": 8006, "kind": "mortar", "pos": [x + 2.0, y, b.nav.gh(x + 2, y)]}
        self.assertEqual(b._take(mk_obs(10.0, me, pickups=[mo, lg]), me, 10.0), {"take": 8005})
        far = {"id": 8007, "kind": "laser", "pos": [x + 9.0, y, b.nav.gh(x + 9, y)]}
        self.assertIsNone(b._take(mk_obs(10.0, me, pickups=[far]), me, 10.0))


class FightTests(unittest.TestCase):
    def _enemies(self, b, me, ms):
        x, y, z = me["pos"]
        out = []
        for m in ms:
            m = dict(m)
            dx, dy = m["pos"][0] - x, m["pos"][1] - y
            m["_dh"] = math.hypot(dx, dy)
            m["_d"] = m["_dh"]
            m["_vis"] = True
            out.append(m)
        out.sort(key=lambda m: m["_d"])
        return out

    def test_provoked_enemy_is_fought_at_range(self):
        x, y = open_point(100)
        b = get_bot()
        me = mk_self(x, y)
        e = self._enemies(b, me, [mk_mech(9, x + 95, y)])
        b.last_hit_by[9] = 9.0
        tgt, mode = b._choose_fight(me, e, [], 10.0)
        self.assertEqual((tgt["id"], mode), (9, "duel"))

    def test_unprovoked_distant_enemy_not_attacked_by_default(self):
        x, y = open_point(100)
        b = get_bot()
        me = mk_self(x, y)
        e = self._enemies(b, me, [mk_mech(9, x + 95, y)])
        tgt, mode = b._choose_fight(me, e, [], 10.0)
        self.assertIsNone(tgt)

    def test_opportunistic_engagement_switch(self):
        x, y = open_point(100)
        b = get_bot()
        me = mk_self(x, y, weapons=[dict(GUN), dict(LIGHTNING)])
        weak = self._enemies(b, me, [mk_mech(9, x + 90, y, hp=500.0)])
        old = CFG["OPP"]
        try:
            CFG["OPP"] = True
            tgt, mode = b._choose_fight(me, weak, [], 10.0)
            self.assertIsNotNone(tgt)
            CFG["OPP"] = False
            tgt, mode = b._choose_fight(me, weak, [], 10.0)
            self.assertIsNotNone(tgt, "a weak (<=30%) target is always finished")
        finally:
            CFG["OPP"] = old

    def test_close_enemy_always_fought_and_nearest_provoked_wins(self):
        x, y = open_point(100)
        b = get_bot()
        me = mk_self(x, y)
        es = self._enemies(b, me, [mk_mech(9, x + 40, y), mk_mech(10, x + 70, y)])
        tgt, mode = b._choose_fight(me, es, [], 10.0)
        self.assertEqual(tgt["id"], 9, "unprovoked but close (<55 m) is still fought")
        b.last_hit_by[10] = 9.5
        b.fight_id = None                           # no committed target yet (a committed one keeps a -15 m hysteresis bonus)
        tgt, mode = b._choose_fight(me, es, [], 10.0)
        self.assertEqual(tgt["id"], 10, "the one that just shot us wins unless it is much farther")
        self.assertEqual(b.fight_id, 10)
        tgt, mode = b._choose_fight(me, es, [], 10.1)
        self.assertEqual(tgt["id"], 10, "and it stays the target")

    def test_retreat_threshold_scales_with_crowd(self):
        x, y = open_point(100)
        b = get_bot()
        hp = 0.33 * 1400.0
        me = mk_self(x, y, hp=hp)
        one = self._enemies(b, me, [mk_mech(9, x + 50, y)])
        b.last_hit_by[9] = 9.9
        tgt, mode = b._choose_fight(me, one, [], 10.0)
        self.assertEqual(mode, "duel", "one enemy and 33% HP: still fighting (threshold 28%)")
        two = self._enemies(b, me, [mk_mech(9, x + 50, y), mk_mech(10, x + 60, y + 5, hp=1400.0)])
        b.recover_until = 0.0
        tgt, mode = b._choose_fight(me, two, [], 10.0)
        self.assertIn(mode, ("flee", "recover"), "two enemies raise the threshold above 33%")

    def test_far_enemy_below_threshold_means_recover_not_fire(self):
        x, y = open_point(140)
        b = get_bot()
        me = mk_self(x, y, hp=0.2 * 1400.0)
        e = self._enemies(b, me, [mk_mech(9, x + 110, y)])
        b.last_hit_by[9] = 9.9
        tgt, mode = b._choose_fight(me, e, [], 10.0)
        self.assertEqual((tgt, mode), (None, "recover"))
        near = self._enemies(b, me, [mk_mech(9, x + 60, y)])
        tgt, mode = b._choose_fight(me, near, [], 10.0)
        self.assertEqual(mode, "flee")

    def test_recover_does_not_flip_back_before_hp_returns(self):
        x, y = open_point(140)
        b = get_bot()
        b.recover_until = 17.0
        me = mk_self(x, y, hp=0.5 * 1400.0)
        tgt, mode = b._choose_fight(me, [], [], 10.0)
        self.assertEqual(mode, "recover")
        me = mk_self(x, y, hp=0.9 * 1400.0)
        tgt, mode = b._choose_fight(me, [], [], 10.0)
        self.assertEqual(mode, "none")

    def test_winning_fight_is_not_abandoned(self):
        x, y = open_point(100)
        b = get_bot()
        me = mk_self(x, y, hp=0.25 * 1400.0, weapons=[dict(GUN), dict(LIGHTNING)])
        e = self._enemies(b, me, [mk_mech(9, x + 20, y, hp=120.0)])
        b.last_hit_by[9] = 9.9
        tgt, mode = b._choose_fight(me, e, [], 10.0)
        self.assertEqual(mode, "duel")

    def test_preferred_range_by_loadout(self):
        b = get_bot()
        gun = mk_self(0, 0)
        lig = mk_self(0, 0, weapons=[dict(GUN), dict(LIGHTNING)])
        las = mk_self(0, 0, weapons=[dict(GUN), dict(LASER)])
        e_plain = mk_mech(9, 10, 0)
        e_lig = mk_mech(9, 10, 0, weapons=["gun", "lightning"])
        self.assertLess(b._pref_range(lig, e_plain), b._pref_range(gun, e_plain))
        self.assertGreater(b._pref_range(las, e_plain), b._pref_range(gun, e_plain))
        self.assertGreaterEqual(b._pref_range(gun, e_lig), 60.0, "gun-only vs lightning: stay out of its 45 m arc")

    def test_multi_enemy_dps_estimate_and_lightning_dps(self):
        b = get_bot()
        e1 = mk_mech(9, 30, 0, weapons=["gun", "lightning"])
        e1["_dh"] = 30.0
        e2 = mk_mech(10, 30, 0)
        e2["_dh"] = 30.0
        self.assertGreater(b._enemy_dps(e1), b._enemy_dps(e2) * 1.8)
        me = mk_self(0, 0, weapons=[dict(GUN), dict(LIGHTNING)])
        me2 = mk_self(0, 0)
        self.assertGreater(b._my_dps(me, 30), 2.0 * b._my_dps(me2, 30))
        self.assertGreater(b._my_dps(me, 30), b._my_dps(me, 80))

    def test_duel_fires_gun_when_aligned_and_lightning_in_cone(self):
        x, y = open_point(60)
        b = get_bot()
        b._zplan = (41.3, -72.8, 380.0)
        me = mk_self(x, y, weapons=[dict(GUN), dict(LIGHTNING)], aim=(0.0, 0.0))
        tg = mk_mech(9, x + 30, y)
        tg["_dh"], tg["_d"], tg["_vis"] = 30.0, 30.0, True
        # pitch toward the target's centre is not exactly 0: set aim equal to the lead solution first
        yaw, pitch, _ = b._lead(me, tg, 252.0)
        me["aim"] = [yaw, max(b.min_pitch, min(b.max_pitch, pitch))]
        mx, my, ayaw, apit, fire, flee = b._duel(mk_obs(10.0, me), me, tg, [tg], (0.0, 0.0), 10.0, "duel")
        self.assertTrue(fire[0] and fire[1])
        me["aim"] = [yaw + 1.0, 0.0]
        *_, fire, flee = b._duel(mk_obs(10.0, me), me, tg, [tg], (0.0, 0.0), 10.0, "duel")
        self.assertFalse(fire[0] or fire[1], "never shoot while the torso is not on target")

    def test_laser_charge_held_then_released_near_full(self):
        x, y = open_point(150)
        b = get_bot()
        b._zplan = (41.3, -72.8, 380.0)
        tg = mk_mech(9, x + 100, y)
        tg["_dh"], tg["_d"], tg["_vis"] = 100.0, 100.0, True
        laser = dict(LASER)
        me = mk_self(x, y, weapons=[dict(GUN), laser])
        yaw, pitch, _ = b._lead(me, tg, 252.0)
        me["aim"] = [yaw, max(b.min_pitch, min(b.max_pitch, pitch))]
        res = []
        for ch in (0.0, 0.3, 0.6, 0.71, 0.75):
            laser["charge"] = ch
            *_, fire, _ = b._duel(mk_obs(10.0, me), me, tg, [tg], (0.0, 0.0), 10.0, "duel")
            res.append(fire[1])
        self.assertEqual(res, [True, True, True, False, False])

    def test_no_laser_start_when_a_second_enemy_is_close(self):
        x, y = open_point(150)
        b = get_bot()
        b._zplan = (41.3, -72.8, 380.0)
        tg = mk_mech(9, x + 100, y)
        tg["_dh"], tg["_d"], tg["_vis"] = 100.0, 100.0, True
        close = mk_mech(10, x + 20, y + 5)
        close["_dh"], close["_d"], close["_vis"] = 20.6, 20.6, True
        me = mk_self(x, y, weapons=[dict(GUN), dict(LASER)])
        yaw, pitch, _ = b._lead(me, tg, 252.0)
        me["aim"] = [yaw, max(b.min_pitch, min(b.max_pitch, pitch))]
        *_, fire, _ = b._duel(mk_obs(10.0, me), me, tg, [tg, close], (0.0, 0.0), 10.0, "duel")
        self.assertFalse(fire[1], "charging halves our speed: not with a second enemy inside 40 m")

    def test_duel_movement_pushes_back_inside_the_zone(self):
        b = get_bot()
        cx, cy, R = ZONE["next_center"][0], ZONE["next_center"][1], 380.0
        x, y = cx + R - 4.0, cy                      # 4 m inside the rim of the circle
        if b.nav.block_at(x, y):
            self.skipTest("rim blocked")
        b._zplan = (cx, cy, R)
        me = mk_self(x, y)
        tg = mk_mech(9, x + 40, y)                   # enemy outside: the strafe + approach would drag us out
        tg["_dh"], tg["_d"], tg["_vis"] = 40.0, 40.0, True
        mx, my, *_ = b._duel(mk_obs(10.0, me), me, tg, [tg], (0.0, 0.0), 10.0, "duel")
        self.assertLess(mx, 0.0, "net movement must point toward the zone centre")


class DropAndSoundTests(unittest.TestCase):
    def test_distant_drop_reachable_before_landing(self):
        x, y = open_point(40)
        b = get_bot()
        dx, dy = x + 150.0, y
        me = mk_self(x, y)
        drop = {"id": 20000, "pos": [dx, dy, 0.0], "radius": 8, "lands_in": 150.0 / (11.7 * 0.85) + 2.0, "capture_s": 4,
                "progress": 0, "holder": None}
        r = b._drop_target(mk_obs(80.0, me, drops=[drop]), me, [], [x + 150.0, y], 380.0, 11.7)
        self.assertIsNotNone(r)
        drop["lands_in"] = 1.0                       # lands long before we could get there... still worth walking to? no
        r = b._drop_target(mk_obs(80.0, me, drops=[drop]), me, [], [x + 150.0, y], 380.0, 11.7)
        self.assertIsNotNone(r, "arrive after landing is fine; arriving early is what the rule avoids")

    def test_drop_skipped_when_contested_or_too_far(self):
        x, y = open_point(40)
        b = get_bot()
        me = mk_self(x, y)
        drop = {"id": 20001, "pos": [x + 150.0, y, 0.0], "radius": 8, "lands_in": 20.0, "capture_s": 4, "progress": 0, "holder": None}
        en = mk_mech(9, x + 150.0, y + 20.0)
        en["_dh"] = 150.0
        r = b._drop_target(mk_obs(80.0, me, drops=[drop]), me, [en], [x + 150.0, y], 380.0, 11.7)
        self.assertIsNone(r)
        drop2 = {"id": 20002, "pos": [x + 340.0, y, 0.0], "radius": 8, "lands_in": 25.0, "capture_s": 4, "progress": 0, "holder": None}
        r = b._drop_target(mk_obs(80.0, me, drops=[drop2]), me, [], [x + 340.0, y], 380.0, 11.7)
        self.assertIsNone(r)

    def test_sound_pre_aim_when_standing_still(self):
        x, y = open_point(40)
        b = get_bot()
        tight = {**ZONE, "center": [x, y], "radius": 40, "next_center": [x, y], "next_radius": 30, "shrink_start": 999, "shrink_end": 1000}
        a = None
        for k in range(600):                         # walk to the hiding spot with a tiny kinematic model
            t = 300.0 + k * 0.1
            b.snd = (t, 2.0)                         # a gun heard to the north-west, refreshed every tick
            a = b.act(mk_obs(t, mk_self(x, y), zone=tight))
            mx, my = a["move"]
            if math.hypot(mx, my) < 0.01:
                break
            n = math.hypot(mx, my)
            sx, sy = x + mx / max(n, 1.0) * 11.7 * 0.1, y + my / max(n, 1.0) * 11.7 * 0.1
            if not b.nav.block_at(sx, sy):
                x, y = sx, sy
        else:
            self.skipTest("never settled at a hide spot in 60 s")
        self.assertAlmostEqual(a["aim"][0], 2.0, places=3)


class ProtocolTests(unittest.TestCase):
    def test_minimal_and_odd_observations(self):
        x, y = open_point(40)
        b = get_bot()
        me = mk_self(x, y)
        o = mk_obs(5.0, me)
        for k in ("sounds", "beams", "crates", "cubes", "pickups", "drops", "projectiles"):
            o.pop(k)
        a = b.act(o)
        self.assertEqual(a["tick"], o["tick"])
        self.assertTrue(all(math.isfinite(v) for v in a["move"] + a["aim"]))
        # odd values: NaN velocity on an enemy, 2-element pickup / cube positions, null weapon slot, dead-on overlap
        en = mk_mech(9, x, y)
        en["vel"] = [float("nan"), 0.0, 0.0]
        o = mk_obs(5.1, mk_self(x, y), mechs=[en], pickups=[{"id": 8100, "kind": "laser", "pos": [x + 2, y]}], cubes=[{"id": 5200, "pos": [x + 1, y]}],
                   crates=[{"id": 1100, "pos": [x + 3, y], "hp": 300}])
        a = b.act(o)
        self.assertTrue(all(math.isfinite(v) for v in a["move"] + a["aim"]))
        self.assertEqual(len(a["fire"]), 2)

    def test_offer_is_answered_with_a_valid_index(self):
        x, y = open_point(40)
        b = get_bot()
        offer = {"id": 1, "expires": 15.0, "choices": [{"id": "servos"}, {"id": "gun_damage"}, {"id": "jets"}]}
        a = b.act(mk_obs(10.0, mk_self(x, y), offer=offer))
        self.assertEqual(a["pick"], 1)
        a = b.act(mk_obs(10.1, mk_self(x, y), offer={"id": 2, "expires": 15.0, "choices": [{"id": "mystery_x"}]}))
        self.assertEqual(a["pick"], 0)

    def test_empty_gun_reloads(self):
        x, y = open_point(40)
        b = get_bot()
        w = dict(GUN)
        w["ammo"] = 0
        a = b.act(mk_obs(10.0, mk_self(x, y, weapons=[w, None])))
        self.assertTrue(a.get("reload"))

    def test_stuck_detector_learns_and_jitters(self):
        x, y = open_point(40)
        b = get_bot()
        n0 = len(b.nav.learned)
        a = None
        for k in range(60):                          # never moves although it keeps asking to walk
            me = mk_self(x, y)
            a = b.act(mk_obs(100.0 + k * 0.1, me, zone={**ZONE, "next_center": [x + 200, y], "next_radius": 100, "shrink_start": 999, "shrink_end": 1000}))
            if b.jitter_until > 100.0 + k * 0.1:
                break
        self.assertGreater(b.jitter_until, 100.0, "stuck -> sidestep jitter engaged")
        self.assertGreaterEqual(len(b.nav.learned), n0)

    def test_decision_latency_budget(self):
        x, y = open_point(60)
        b = get_bot()
        times = []
        en = mk_mech(9, x + 40, y)
        crate = {"id": 1200, "pos": [x + 25, y + 4, b.nav.gh(x + 25, y + 4)], "hp": 300.0}
        for k in range(600):
            me = mk_self(x + (k % 7) * 0.2, y)
            o = mk_obs(10.0 + k * 0.1, me, mechs=[en] if k % 3 else [], crates=[crate])
            t0 = time.perf_counter()
            b.act(o)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        self.assertLess(times[int(0.99 * len(times))], 10.0, "p99 must stay far below the 50 ms budget")


if __name__ == "__main__":
    unittest.main(verbosity=2)
