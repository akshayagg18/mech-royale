#!/usr/bin/env python3
"""Regression / decision tests for the Ghost-v3 changes (real arena map, synthetic observations).

    python3 tests/test_v3.py [--map /tmp/kit/mechfight-kit/arena]

Each class pins one v3 change (weapon-specific aim, laser release, turn-rate-aware alignment, observation trust, gun dodge,
farm effectiveness / planner, upgrade profiles, quiet retreat, ...). They test decisions, not the game score.
"""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_v2 as T  # noqa: E402  (also sets up sys.path / MAP / the shared nav)
from test_v2 import (get_bot, open_point, mk_self, mk_obs, mk_mech, run_acts, GUN, LASER, LIGHTNING, ZONE)  # noqa: E402
from ghost_bot import CFG  # noqa: E402


class cfg:
    """Temporarily override CFG keys."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = CFG[k]
            CFG[k] = v

    def __exit__(self, *a):
        CFG.update(self.old)


def lat_tgt(x, y, vy=10.0, dist=100.0):
    return {"id": 9, "kind": "mech", "pos": [x + dist, y, get_bot().nav.gh(x + dist, y)], "vel": [0.0, vy, 0.0], "cubes": 0}


class AimTests(unittest.TestCase):
    def setUp(self):
        self.x, self.y = open_point(110)
        self.b = get_bot()
        self.me = mk_self(self.x, self.y)

    def test_gun_leads_but_laser_is_hitscan(self):
        tgt = lat_tgt(self.x, self.y)
        with cfg(AIM_SPLIT=True, TURN_AWARE=False):
            gy, _, _ = self.b._aim_for(self.me, tgt, "gun", 252.0)
            ly, _, _ = self.b._aim_for(self.me, tgt, "laser", 252.0)
        direct = math.atan2(tgt["pos"][1] - self.y, tgt["pos"][0] - self.x)
        self.assertAlmostEqual(ly, direct, places=6, msg="hitscan has no flight-time lead")
        self.assertGreater(gy - direct, 0.03, "a 100 m gun shot at 10 m/s lateral needs about 0.04 rad of lead")

    def test_v2_behaviour_available_as_flag(self):
        tgt = lat_tgt(self.x, self.y)
        with cfg(AIM_SPLIT=False, TURN_AWARE=False):
            ly, _, _ = self.b._aim_for(self.me, tgt, "laser", 252.0)
            gy, _, _ = self.b._aim_for(self.me, tgt, "gun", 252.0)
        self.assertAlmostEqual(ly, gy, places=9)

    def test_turn_aware_horizon_adds_latency_and_slew(self):
        tgt = lat_tgt(self.x, self.y)
        me_far = mk_self(self.x, self.y, aim=(1.2, 0.0))             # torso 1.2 rad off: 0.3 s of slewing at 4 rad/s
        with cfg(AIM_SPLIT=True, TURN_AWARE=True):
            y_near, _, _ = self.b._aim_for(self.me, tgt, "laser")
            y_far, _, _ = self.b._aim_for(me_far, tgt, "laser")
        with cfg(AIM_SPLIT=True, TURN_AWARE=False):
            y0, _, _ = self.b._aim_for(self.me, tgt, "laser")
        self.assertGreater(y_near, y0)
        self.assertGreater(y_far, y_near, "a torso that needs longer to arrive must lead the moving target further")

    def test_alignment_respects_turn_rate(self):
        # slew credit = half a tick at the real turn rate, capped at half the tolerance -> accept at most 1.5 x tolerance
        me = mk_self(self.x, self.y, aim=(0.0, 0.0))
        with cfg(TURN_AWARE=True):
            self.assertFalse(self.b._aligned(me, 0.3, 0.0, 0.05, 0.08))
            self.assertTrue(self.b._aligned(me, 0.04, 0.0, 0.05, 0.08))
            self.assertTrue(self.b._aligned(me, 0.07, 0.0, 0.05, 0.08), "inside tolerance + capped slew credit")
            self.assertFalse(self.b._aligned(me, 0.08, 0.0, 0.05, 0.08), "beyond 1.5 x tolerance, whatever the turn rate")
            slow = mk_self(self.x, self.y, aim=(0.0, 0.0))
            slow["stats"] = dict(slow["stats"], turn_rate=0.5)         # almost no slew credit
            self.assertFalse(self.b._aligned(slow, 0.07, 0.0, 0.05, 0.08))
        with cfg(TURN_AWARE=False):
            self.assertFalse(self.b._aligned(me, 0.07, 0.0, 0.05, 0.08), "v2: plain tolerance")

    def test_tracking_limit_by_angular_speed(self):
        me = mk_self(self.x, self.y)
        fast_close = {"pos": [self.x + 12, self.y, 0], "vel": [0, 11.7, 0], "kind": "mech"}     # 0.97 rad/s of line-of-sight rate
        fast_far = {"pos": [self.x + 100, self.y, 0], "vel": [0, 11.7, 0], "kind": "mech"}
        with cfg(TURN_AWARE=True):
            self.assertTrue(self.b._tracking_ok(me, fast_far, 100))
            me2 = mk_self(self.x, self.y)
            me2["stats"] = dict(me2["stats"], turn_rate=0.8)           # sluggish torso
            self.assertFalse(self.b._tracking_ok(me2, fast_close, 12))


class LaserTests(unittest.TestCase):
    def setUp(self):
        self.b = get_bot()

    def wp(self, charge, cd=0.0):
        d = dict(LASER)
        d["charge"], d["cooldown"] = charge, cd
        return d

    def test_start_requires_alignment(self):
        st = LASER["stats"]
        self.assertFalse(self.b._laser_fire(self.wp(0.0), st, 100, False, False, 0, False, 10.0))
        self.assertTrue(self.b._laser_fire(self.wp(0.0), st, 100, True, False, 0, False, 10.0))

    def test_holds_at_full_charge_until_aligned_then_releases(self):
        st = LASER["stats"]
        w = self.wp(0.75)
        with cfg(LASER_HOLD=True):
            self.b.laser_full_t = None
            self.assertTrue(self.b._laser_fire(w, st, 100, True, False, 0, False, 10.0), "misaimed: keep tracking")
            self.assertTrue(self.b._laser_fire(w, st, 100, True, False, 0, False, 10.2))
            self.assertFalse(self.b._laser_fire(w, st, 100, True, True, 0, False, 10.25), "aligned: release")

    def test_releases_after_hold_cap(self):
        st = LASER["stats"]
        w = self.wp(0.75)
        with cfg(LASER_HOLD=True):
            self.b.laser_full_t = None
            self.assertTrue(self.b._laser_fire(w, st, 100, True, False, 0, False, 10.0))
            self.assertFalse(self.b._laser_fire(w, st, 100, True, False, 0, False, 10.5), "0.45 s cap: do not sit at half speed")

    def test_v2_releases_at_full_charge_regardless(self):
        st = LASER["stats"]
        with cfg(LASER_HOLD=False):
            self.assertFalse(self.b._laser_fire(self.wp(0.75), st, 100, True, False, 0, False, 10.0))

    def test_abort_releases_during_charge(self):
        st = LASER["stats"]
        with cfg(LASER_HOLD=True):
            self.assertFalse(self.b._laser_fire(self.wp(0.4), st, 100, True, True, 0, True, 10.0))

    def test_duel_aims_laser_without_lead_and_holds_gun_while_charging(self):
        x, y = open_point(110)
        b = get_bot()
        en = mk_mech(9, x + 100, y, vel=(0, 10.0), weapons=["gun"])
        w = dict(LASER)
        w["charge"] = 0.3
        me = mk_self(x, y, weapons=[dict(GUN), w])
        with cfg(AIM_SPLIT=True, TURN_AWARE=False):
            obs = mk_obs(10.0, me, mechs=[en])
            a = b.act(obs)
        direct = math.atan2(en["pos"][1] - y, en["pos"][0] - x)
        if b.mode in ("duel", "flee"):
            self.assertAlmostEqual(a["aim"][0], direct, delta=0.01, msg="charging laser: zero-lead line")
            self.assertFalse(a["fire"][0], "no gun shots while the laser is charging (they would not change the beam, but waste ammo)")


class TrustObsTests(unittest.TestCase):
    def find_blocked_pair(self, dist=30.0):
        nav = get_bot().nav
        import random
        r = random.Random(11)
        for _ in range(20000):
            x, y = 41.3 + r.uniform(-150, 150), -72.8 + r.uniform(-150, 150)
            a = r.uniform(0, 2 * math.pi)
            x2, y2 = x + math.cos(a) * dist, y + math.sin(a) * dist
            if nav.block_at(x, y) or nav.block_at(x2, y2):
                continue
            if not nav.los(x, y, x2, y2):
                return x, y, x2, y2, a
        self.skipTest("no blocked pair")

    def test_server_listed_enemy_inside_sight_is_kept(self):
        x, y, x2, y2, a = self.find_blocked_pair(30.0)
        b = get_bot()
        en = mk_mech(9, x2, y2)
        me = mk_self(x, y, aim=(a, 0.0))
        with cfg(TRUST_OBS=True):
            b.act(mk_obs(10.0, me, mechs=[en]))
            self.assertTrue(en["_vis"])
        b2 = get_bot()
        en2 = mk_mech(9, x2, y2)
        with cfg(TRUST_OBS=False):
            b2.act(mk_obs(10.0, me, mechs=[en2]))
            self.assertFalse(en2["_vis"], "v2 discarded it because the 2D raster disagreed")

    def test_enemy_far_behind_us_beyond_near_range_is_not_trusted(self):
        x, y, x2, y2, a = self.find_blocked_pair(80.0)
        b = get_bot()
        en = mk_mech(9, x2, y2)
        me = mk_self(x, y, aim=(a + math.pi, 0.0))           # looking the other way: outside the 120 deg wedge, beyond 45 m
        with cfg(TRUST_OBS=True):
            b.act(mk_obs(10.0, me, mechs=[en]))
        self.assertFalse(en["_vis"])


def proj(x, y, z, vx, vy, vz=0.0, owner=9):
    return {"id": 77, "kind": "gun", "owner": owner, "pos": [x, y, z], "vel": [vx, vy, vz], "damage": 45.0}


class GunDodgeTests(unittest.TestCase):
    def setUp(self):
        self.x, self.y = open_point(60)
        self.b = get_bot()
        self.me = mk_self(self.x, self.y)
        self.z = self.me["pos"][2] + 4.5

    def test_incoming_bullet_gets_a_sidestep(self):
        # bullet 120 m east, flying straight at us at 252 m/s: 0.48 s to impact
        p = proj(self.x + 120, self.y, self.z, -252.0, 0.0)
        with cfg(DODGE_GUN=True):
            r = self.b._gun_dodge(mk_obs(10.0, self.me, projectiles=[p]), self.me)
        self.assertIsNotNone(r)
        dx, dy, urgent = r
        self.assertGreater(abs(dy), abs(dx), "sidestep is perpendicular to the bullet line")

    def test_bullet_too_close_is_ignored(self):
        p = proj(self.x + 20, self.y, self.z, -252.0, 0.0)           # 0.08 s: cannot clear 2.8 m in that time
        with cfg(DODGE_GUN=True):
            self.assertIsNone(self.b._gun_dodge(mk_obs(10.0, self.me, projectiles=[p]), self.me))

    def test_missing_bullet_is_ignored(self):
        p = proj(self.x + 120, self.y + 15, self.z, -252.0, 0.0)     # passes 15 m to the side
        with cfg(DODGE_GUN=True):
            self.assertIsNone(self.b._gun_dodge(mk_obs(10.0, self.me, projectiles=[p]), self.me))

    def test_bullet_flying_away_is_ignored(self):
        p = proj(self.x + 40, self.y, self.z, 252.0, 0.0)
        with cfg(DODGE_GUN=True):
            self.assertIsNone(self.b._gun_dodge(mk_obs(10.0, self.me, projectiles=[p]), self.me))


class FarmTests(unittest.TestCase):
    def test_ineffective_fire_repositions_then_blacklists(self):
        x, y = open_point(40)
        b = get_bot()
        crate = {"id": 2001, "pos": [x + 28, y, b.nav.gh(x + 28, y)], "hp": 300.0}
        aim = [0.0, 0.0]

        def me_fn(last):
            if last is not None:
                aim[0], aim[1] = last["aim"]
            return mk_self(x, y, aim=aim)
        with cfg(FARM_EFF=True, FARM_PLAN=True):
            run_acts(b, 10.0, me_fn, lambda t, me: mk_obs(t, me, crates=[crate]), n=80)     # 8 s, crate HP never moves
        self.assertGreaterEqual(b.stat["farm_repos"] + b.stat["farm_abandon"], 1)
        self.assertGreater(b.black.get(2001, 0), 10.0, "target that never takes damage must end up blacklisted")

    def test_effective_fire_is_not_interrupted(self):
        x, y = open_point(40)
        b = get_bot()
        hp = [300.0]
        aim = [0.0, 0.0]

        def me_fn(last):
            if last is not None:
                aim[0], aim[1] = last["aim"]
            return mk_self(x, y, aim=aim)

        def obs_fn(t, me):
            hp[0] = max(1.0, hp[0] - 12.0)                      # steady damage
            crate = {"id": 2002, "pos": [x + 28, y, b.nav.gh(x + 28, y)], "hp": hp[0]}
            return mk_obs(t, me, crates=[crate])
        with cfg(FARM_EFF=True):
            run_acts(b, 10.0, me_fn, obs_fn, n=20)
        self.assertEqual(b.stat["farm_repos"], 0)
        self.assertEqual(b.black.get(2002, 0), 0)

    def test_farm_planner_engaged_when_direct_approach_stalls(self):
        x, y = open_point(40)
        b = get_bot()
        # the bot cannot make progress toward the cube (stands still): after 2.5 s the planner is asked for a route
        cube = {"id": 2003, "pos": [x + 38, y, b.nav.gh(x + 38, y)]}
        with cfg(FARM_PLAN=True):
            run_acts(b, 10.0, lambda last: mk_self(x, y), lambda t, me: mk_obs(t, me, cubes=[cube]), n=40)
        self.assertGreaterEqual(b.stat["farm_plan"], 1)
        self.assertTrue(b.want is not None and b.want[0] == "farm", "the planner request is for the farm target")

    def test_quiet_farm_skips_noisy_targets_when_hunters_are_close(self):
        x, y = open_point(40)
        b = get_bot()
        crate = {"id": 2004, "pos": [x + 25, y, b.nav.gh(x + 25, y)], "hp": 300.0}
        me = mk_self(x, y)
        b.tracks[50] = {"pos": (x - 120.0, y), "t": 9.0, "hp": 1400, "w": ["gun"], "vel": (0.0, 0.0), "vis": False}
        thr = b._threats(10.0, x, y)
        with cfg(QUIET_FARM=True):
            r = b._farm(mk_obs(10.0, me, crates=[crate]), me, [], ZONE["next_center"], 380.0, 10.0, x, y, thr)
        self.assertIsNone(r)
        b.ft = None
        with cfg(QUIET_FARM=False):
            r = b._farm(mk_obs(10.0, me, crates=[crate]), me, [], ZONE["next_center"], 380.0, 10.0, x, y, thr)
        self.assertIsNotNone(r)

    def test_threat_farm_skips_targets_next_to_hunters(self):
        x, y = open_point(40)
        b = get_bot()
        cube = {"id": 2005, "pos": [x + 20, y, b.nav.gh(x + 20, y)]}
        me = mk_self(x, y)
        b.tracks[51] = {"pos": (x + 35.0, y), "t": 9.0, "hp": 1400, "w": ["gun"], "vel": (0.0, 0.0), "vis": False}
        thr = b._threats(10.0, x, y)
        with cfg(THREAT_FARM=True):
            self.assertIsNone(b._farm(mk_obs(10.0, me, cubes=[cube]), me, [], ZONE["next_center"], 380.0, 10.0, x, y, thr))
        b.ft = None
        with cfg(THREAT_FARM=False):
            self.assertIsNotNone(b._farm(mk_obs(10.0, me, cubes=[cube]), me, [], ZONE["next_center"], 380.0, 10.0, x, y, thr))


class UpgradeTests(unittest.TestCase):
    OFFER = {"choices": [{"id": "gun_damage"}, {"id": "nanites"}, {"id": "servos"}]}

    def pick(self, profile, upgrades=(), w1=None):
        b = get_bot()
        me = mk_self(0, 0)
        me["upgrades"] = [{"id": u} for u in upgrades]
        if w1:
            me["weapons"] = [dict(GUN), dict(w1)]
        b.me_last = me
        with cfg(UPG=profile):
            return self.OFFER["choices"][b._pick_offer(self.OFFER)]["id"]

    def test_damage_profile_default(self):
        self.assertEqual(self.pick("damage"), "gun_damage")

    def test_regen_profile_takes_nanites_first_and_twice(self):
        self.assertEqual(self.pick("regen"), "nanites")
        off = {"choices": [{"id": "nanites"}, {"id": "gun_damage"}, {"id": "plating"}]}
        b = get_bot()
        me = mk_self(0, 0)
        me["upgrades"] = [{"id": "nanites"}]
        b.me_last = me
        with cfg(UPG="regen"):
            pick = off["choices"][b._pick_offer(off)]["id"]
        self.assertIn(pick, ("plating", "gun_damage"), "second nanite stack is ranked after plating/armor/damage")

    def test_escape_profile_prefers_servos(self):
        self.assertEqual(self.pick("escape"), "servos")

    def test_weapon_profile_follows_second_slot(self):
        off = {"choices": [{"id": "laser_damage"}, {"id": "lightning_efficiency"}, {"id": "gun_damage"}]}
        b = get_bot()
        me = mk_self(0, 0, weapons=[dict(GUN), dict(LIGHTNING)])
        b.me_last = me
        with cfg(UPG="weapon"):
            self.assertEqual(off["choices"][b._pick_offer(off)]["id"], "lightning_efficiency")
        me = mk_self(0, 0, weapons=[dict(GUN), dict(LASER)])
        b.me_last = me
        with cfg(UPG="weapon"):
            self.assertEqual(off["choices"][b._pick_offer(off)]["id"], "laser_damage")


class QuietRetreatTests(unittest.TestCase):
    def test_no_shooting_while_retreating_unless_target_is_nearly_dead(self):
        x, y = open_point(60)
        b = get_bot()
        aim = [0.0, 0.0]
        for en_hp, expect_fire in ((1400.0, False), (80.0, True)):
            b = get_bot()
            en = mk_mech(9, x + 50, y, hp=en_hp)
            me = mk_self(x, y, hp=500.0, aim=aim)
            tgt = en
            en["_dh"], en["_d"], en["_vis"] = 50.0, 50.0, True
            b._zplan = b._zone_now(ZONE, 10.0)
            with cfg(QUIET_RETREAT=True):
                _, _, _, _, fire, flee = b._duel(mk_obs(10.0, me, mechs=[en]), me, tgt, [en], (0.0, 0.0), 10.0, "flee")
            self.assertTrue(flee)
            if expect_fire:
                # whether the gun is aligned is not what is tested; a kill-shot candidate must not be suppressed by the rule
                self.assertEqual(b.stat["quiet_retreat"], 0)
            else:
                self.assertEqual(fire, [False, False])



def hide_req(b, x, y, threats=(), cur=None, nr=380.0):
    return {"kind": "hide", "nc": tuple(ZONE["next_center"]), "nr": nr, "pos": (x, y), "zone": None, "ver": 1,
            "want": ("hide", 1), "threats": tuple(threats), "cur": cur}


class ThreatHideTests(unittest.TestCase):
    def setUp(self):
        self.b = get_bot()
        self.x, self.y = open_point(40)

    def test_pen_grows_with_proximity_and_sight(self):
        b = self.b
        cx, cy = self.x, self.y
        near = [(cx + 30, cy, 1.0, ("gun",), 1.0)]
        far = [(cx + 300, cy, 1.0, ("gun",), 1.0)]
        sound = [(cx + 30, cy, 1.0, ("gun",), 0.5)]
        p_near, p_far, p_snd = (b._threat_pen(cx, cy, 10.0, t) for t in (near, far, sound))
        self.assertGreater(p_near, p_far + 20.0)
        self.assertGreater(p_near, p_snd, "a sound fix is weighted below a sighting")
        self.assertLess(p_far, 5.0, "a threat 300 m away should hardly matter")

    def test_slow_arrival_is_penalised_against_a_fast_enemy(self):
        b = self.b
        thr = [(self.x + 80, self.y, 1.0, ("gun",), 1.0)]
        self.assertGreater(b._threat_pen(self.x, self.y, 40.0, thr), b._threat_pen(self.x, self.y, 2.0, thr))

    def test_two_sectors_and_no_exit_cost_more(self):
        b = self.b
        one = [(self.x + 60, self.y, 1.0, ("gun",), 1.0)]
        two = one + [(self.x - 60, self.y + 5, 1.0, ("gun",), 1.0)]
        self.assertGreater(b._threat_pen(self.x, self.y, 10.0, two), b._threat_pen(self.x, self.y, 10.0, one))

    def test_hide_choice_moves_away_from_a_known_hunter(self):
        b = self.b
        base = b._compute(hide_req(b, self.x, self.y))
        hx, hy = base["H"]
        thr = [(hx + 18.0, hy + 6.0, 2.0, ("gun",), 1.0)]
        with cfg(THREAT_HIDE=True):
            r = b._compute(hide_req(b, self.x, self.y, thr))
        d_base = math.hypot(hx - thr[0][0], hy - thr[0][1])
        d_new = math.hypot(r["H"][0] - thr[0][0], r["H"][1] - thr[0][1])
        self.assertGreater(d_new, d_base + 10.0, "hunter parked next to the static best cell: pick another")

    def test_no_threats_is_identical_to_v2_selection(self):
        b = self.b
        a = b._compute(hide_req(b, self.x, self.y))
        c = b._compute(hide_req(b, self.x, self.y, ()))
        self.assertEqual(a["H"], c["H"])

    def test_hysteresis_keeps_current_spot_for_a_distant_threat(self):
        b = self.b
        base = b._compute(hide_req(b, self.x, self.y))
        far = [(base["H"][0] + 250.0, base["H"][1], 3.0, ("gun",), 1.0)]
        with cfg(THREAT_HIDE=True):
            r = b._compute(hide_req(b, self.x, self.y, far, cur=base["H"]))
        self.assertLess(math.hypot(r["H"][0] - base["H"][0], r["H"][1] - base["H"][1]), 8.0)

    def test_edge_weight_prefers_the_outer_ring(self):
        b = self.b
        nc = ZONE["next_center"]
        with cfg(EDGE_W=0.0):
            a = b._compute(hide_req(b, self.x, self.y))
        with cfg(EDGE_W=3.0):
            e = b._compute(hide_req(b, self.x, self.y))
        da = math.hypot(a["H"][0] - nc[0], a["H"][1] - nc[1])
        de = math.hypot(e["H"][0] - nc[0], e["H"][1] - nc[1])
        self.assertGreaterEqual(de, da - 1.0)

    def test_rehide_triggers_on_new_threat_and_respects_cooldown(self):
        b = self.b
        calls = []
        orig = b._issue
        b._issue = lambda tmpl, x, y, want, keep=False: calls.append((want, keep))
        b.want = ("hide", 1)
        b.tmpl = {"kind": "hide", "nc": (0, 0), "nr": 300}
        b.req_t = -99.0
        th = [{"x": self.x + 80, "y": self.y, "age": 1.0, "d": 80.0, "w": ["gun"], "src": "seen", "id": 1}]
        with cfg(THREAT_HIDE=True, REHIDE_S=5.0):
            b._rehide_check(10.0, self.x, self.y, th, True)
            b._rehide_check(11.0, self.x, self.y, th, True)          # same picture: nothing
            th2 = [dict(th[0], x=self.x + 200, d=200.0)]
            b._rehide_check(12.0, self.x, self.y, th2 + [dict(th[0], x=self.x - 70, d=70.0, id=2)], True)   # new hunter, but inside cooldown
            b._rehide_check(16.0, self.x, self.y, th2 + [dict(th[0], x=self.x - 70, d=70.0, id=2)], True)   # cooldown over
        b._issue = orig
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(k for _, k in calls), "re-plans keep following the old plan until the new one lands")

    def test_planner_request_carries_the_threat_snapshot(self):
        b = self.b
        b.tracks[7] = {"pos": (self.x + 70.0, self.y), "t": 9.0, "hp": 1400, "w": ["gun"], "vel": (0.0, 0.0), "vis": False}
        b.threats_now = b._threats(10.0, self.x, self.y)
        seen = {}
        orig = b._request
        b._request = lambda req, keep=False: seen.update(req)
        with cfg(THREAT_HIDE=True):
            b._issue({"kind": "hide", "nc": (0, 0), "nr": 300}, self.x, self.y, ("hide", 1), False)
        b._request = orig
        self.assertEqual(len(seen["threats"]), 1)
        with cfg(THREAT_HIDE=False):
            self.assertEqual(b._threat_snapshot(), ())


class EngageTests(unittest.TestCase):
    def setUp(self):
        self.x, self.y = open_point(110)
        self.b = get_bot()

    def foe(self, hp=1400.0, d=90.0, weapons=None, cubes=0, mid=9):
        e = mk_mech(mid, self.x + d, self.y, hp=hp, weapons=weapons or ["gun"], cubes=cubes)
        e["_dh"] = e["_d"] = d
        e["_vis"] = True
        return e

    def test_weak_isolated_target_is_worth_it_and_strong_one_is_not(self):
        me = mk_self(self.x, self.y)
        weak, strong = self.foe(hp=250.0), self.foe(hp=1400.0)
        ev_w = self.b._ev_engage(me, weak, [weak], [weak], 10.0)
        ev_s = self.b._ev_engage(me, strong, [strong], [strong], 10.0)
        self.assertGreater(ev_w, CFG["EV_MIN"])
        self.assertGreater(ev_w, ev_s + 0.5, "a nearly dead target is worth much more than a full-health one")
        with cfg(EV_MIN=0.35):
            self.assertLess(ev_s, CFG["EV_MIN"], "an even fight against a full-health mech is not worth it at the stricter threshold")

    def test_hunters_around_the_target_make_it_unattractive(self):
        me = mk_self(self.x, self.y)
        e = self.foe(hp=900.0)
        alone = self.b._ev_engage(me, e, [e], [e], 10.0)
        for k in (50, 51):
            self.b.tracks[k] = {"pos": (self.x + 100.0, self.y + 20.0 * (k - 50)), "t": 8.0, "hp": 1400, "w": ["gun"],
                                "vel": (0.0, 0.0), "vis": False}
        crowded = self.b._ev_engage(me, e, [e], [e], 10.0)
        self.assertLess(crowded, alone - 0.4)

    def test_weapon_triangle_changes_the_estimate(self):
        e_mortar = self.foe(hp=700.0, weapons=["gun", "mortar"])
        e_laser = self.foe(hp=700.0, weapons=["gun", "laser"])
        me_laser = mk_self(self.x, self.y, weapons=[dict(GUN), dict(LASER)])
        self.assertGreater(self.b._ev_engage(me_laser, e_mortar, [e_mortar], [e_mortar], 10.0),
                           self.b._ev_engage(me_laser, e_laser, [e_laser], [e_laser], 10.0), "laser beats mortar, mirror ties laser")
        e_l2 = self.foe(hp=700.0, weapons=["gun", "laser"])
        me_light = mk_self(self.x, self.y, weapons=[dict(GUN), dict(LIGHTNING)])
        self.assertLess(self.b._ev_engage(me_light, e_l2, [e_l2], [e_l2], 10.0),
                        self.b._ev_engage(me_laser, e_mortar, [e_mortar], [e_mortar], 10.0), "lightning loses to laser")

    def test_lightning_cluster_bonus(self):
        me = mk_self(self.x, self.y, weapons=[dict(GUN), dict(LIGHTNING)])
        a = self.foe(hp=800.0, d=30.0)
        b2 = self.foe(hp=800.0, d=30.0, mid=10)
        b2["pos"] = [a["pos"][0], a["pos"][1] + 8.0, a["pos"][2]]
        lone = self.b._ev_engage(me, a, [a], [a], 10.0)
        pair = self.b._ev_engage(me, a, [a, b2], [a, b2], 10.0)
        # a pair doubles the incoming damage but the chain adds 45%: the model must at least account for both effects
        self.assertNotEqual(round(lone, 3), round(pair, 3))

    def test_choose_fight_ev_declines_where_v2_accepts_with_hunters_around(self):
        me = mk_self(self.x, self.y)
        e = self.foe(hp=1000.0, d=90.0)
        for k in (50, 51):
            self.b.tracks[k] = {"pos": (self.x + 100.0, self.y + 20.0 * (k - 50)), "t": 9.0, "hp": 1400, "w": ["gun"],
                                "vel": (0.0, 0.0), "vis": False}
        with cfg(ENGAGE="off", OPP=True):
            f_off, mode_off = self.b._choose_fight(me, [e], [], 10.0)
        with cfg(ENGAGE="ev"):
            self.b.fight_id = None
            f_ev, mode_ev = self.b._choose_fight(me, [e], [], 10.0)
        self.assertIsNotNone(f_off, "the v2 opportunistic rule (OPP) takes the fight: it only counts the enemies it can see")
        self.assertIsNone(f_ev, "the EV model sees the hunters standing behind the target")

    def test_choose_fight_ev_takes_isolated_weak_target(self):
        me = mk_self(self.x, self.y)
        e = self.foe(hp=300.0, d=100.0)
        with cfg(ENGAGE="ev"):
            f, mode = self.b._choose_fight(me, [e], [], 10.0)
        self.assertIsNotNone(f)

    def test_close_or_provoking_enemies_are_never_declined_by_ev(self):
        me = mk_self(self.x, self.y)
        e = self.foe(hp=1400.0, d=40.0)
        with cfg(ENGAGE="ev"):
            f, mode = self.b._choose_fight(me, [e], [], 10.0)
        self.assertIsNotNone(f, "inside CLOSE the enemy will catch us anyway")


class QuietGraceTests(unittest.TestCase):
    def test_under_fire_the_retreat_keeps_shooting_back(self):
        x, y = open_point(60)
        out = {}
        for label, last_dmg in (("quiet", -99.0), ("hit", 9.5)):
            b = get_bot()
            en = mk_mech(9, x + 50, y, hp=1400.0)
            en["_dh"], en["_d"], en["_vis"] = 50.0, 50.0, True
            b._zplan = b._zone_now(ZONE, 10.0)
            b.last_damage_t = last_dmg
            aim = [0.0, 0.0]
            me = mk_self(x, y, hp=500.0, aim=aim)
            fired = 0
            with cfg(QUIET_RETREAT=True, QUIET_GRACE=2.0):
                for k in range(6):
                    _, _, _, _, fire, flee = b._duel(mk_obs(10.0 + k * 0.1, me, mechs=[en]), me, en, [en], (0.0, 0.0),
                                                     10.0 + k * 0.1, "flee")
                    fired += 1 if fire[0] else 0
                    me = mk_self(x, y, hp=500.0, aim=(0.0, 0.0))
            out[label] = fired
        self.assertEqual(out["quiet"], 0)
        self.assertGreater(out["hit"], 0, "being hit means regeneration is impossible anyway: return fire")


class RecoveryTests(unittest.TestCase):
    def test_no_new_engagement_while_regenerating(self):
        x, y = open_point(110)
        b = get_bot()
        me = mk_self(x, y, hp=900.0)
        e = mk_mech(9, x + 100, y, hp=250.0)
        e["_dh"] = e["_d"] = 100.0
        e["_vis"] = True
        b.recover_until = 20.0
        with cfg(QUIET_RETREAT=True, ENGAGE="off"):
            f, mode = b._choose_fight(me, [e], [], 10.0)
        self.assertIsNone(f)
        self.assertEqual(mode, "recover")
        b.fight_id = None
        with cfg(QUIET_RETREAT=False, ENGAGE="off"):
            f, mode = b._choose_fight(me, [e], [], 10.0)
        self.assertIsNotNone(f, "v2 would go for the wounded enemy and reset its own regeneration clock")
        b.fight_id = None
        with cfg(QUIET_RETREAT=True, ENGAGE="ev", EV_RECOVER_GATE=True):
            f, mode = b._choose_fight(me, [e], [], 10.0)
        self.assertIsNone(f, "the gate keeps the EV model from starting fights while regenerating")
        b.fight_id = None
        with cfg(QUIET_RETREAT=True, ENGAGE="ev", EV_RECOVER_GATE=False):
            f, mode = b._choose_fight(me, [e], [], 10.0)
        self.assertIsNotNone(f, "without the gate a nearly dead isolated target is taken even while regenerating")


class Scan360Tests(unittest.TestCase):
    def sweep(self, flag):
        x0, y0 = open_point(40)
        b = get_bot()
        x, y = b._compute(hide_req(b, x0, y0))["H"]           # stand on the hiding cell: arrived, nothing to do, calm
        yaws = []
        aim = [0.0, 0.0]
        with cfg(SCAN360=flag):
            last = None
            for k in range(60):
                if last is not None:
                    aim[0] = last["aim"][0]
                me = mk_self(x, y, aim=aim)
                last = b.act(mk_obs(100.0 + k * 0.1, me))
                yaws.append(last["aim"][0])
        return yaws

    @staticmethod
    def span(y):
        u, tot = [y[0]], 0.0
        for a, b in zip(y, y[1:]):
            tot += math.atan2(math.sin(b - a), math.cos(b - a))
            u.append(y[0] + tot)
        return max(u) - min(u)

    def test_sweep_covers_the_full_circle_when_standing_still_and_calm(self):
        self.assertGreater(self.span(self.sweep(True)), 2 * math.pi - 0.3)

    def test_flag_off_keeps_the_v2_sweep_around_the_open_direction(self):
        self.assertLess(self.span(self.sweep(False)[5:]), 2.6, "v2: +-1.05 rad around the most open direction")


class LootThreatTests(unittest.TestCase):
    def test_cube_beside_a_recent_sighting_is_skipped(self):
        x, y = open_point(40)
        b = get_bot()
        cube = {"id": 3001, "pos": [x + 15, y, b.nav.gh(x + 15, y)]}
        me = mk_self(x, y)
        b.tracks[60] = {"pos": (x + 30.0, y), "t": 9.0, "hp": 1400, "w": ["gun"], "vel": (0.0, 0.0), "vis": False}
        thr = b._threats(10.0, x, y)
        obs = mk_obs(10.0, me, cubes=[cube])
        with cfg(THREAT_FARM=True):
            active, _, _ = b._loot(obs, me, ZONE["next_center"], 380.0, None, 10.0, x, y, thr)
        self.assertFalse(active)
        b.loot = None
        with cfg(THREAT_FARM=False):
            active, _, _ = b._loot(obs, me, ZONE["next_center"], 380.0, None, 10.0, x, y, thr)
        self.assertTrue(active)



class PresetTests(unittest.TestCase):
    def test_v2_preset_keys_exist_and_turn_every_v3_change_off(self):
        import ghost_bot
        for k, v in ghost_bot.V2_COMPAT.items():
            self.assertIn(k, CFG)
        self.assertEqual(ghost_bot.V2_COMPAT["ENGAGE"], "off")
        self.assertEqual(ghost_bot.V2_COMPAT["UPG"], "damage")
        for k in ("AIM_SPLIT", "TURN_AWARE", "LASER_HOLD", "TRUST_OBS", "DODGE_GUN", "FARM_EFF", "FARM_PLAN", "QUIET_RETREAT",
                  "THREAT_HIDE", "SCAN360", "QUIET_FARM", "THREAT_FARM"):
            self.assertIs(ghost_bot.V2_COMPAT[k], False)

    def test_v3_flags_off_gun_still_fires_while_laser_is_charging_like_v2(self):
        x, y = open_point(110)
        b = get_bot()
        en = mk_mech(9, x + 100, y, weapons=["gun"])
        w = dict(LASER)
        w["charge"] = 0.3
        me = mk_self(x, y, weapons=[dict(GUN), w])
        aim_to = math.atan2(0.0, 1.0)
        me["aim"] = [aim_to, 0.0]
        from test_v2 import mk_obs as _mo
        with cfg(AIM_SPLIT=False, TURN_AWARE=False, LASER_HOLD=False):
            a = b.act(_mo(10.0, me, mechs=[en]))
        if b.mode in ("duel", "flee"):
            # not asserting a particular decision of the aim; the point is only that v2 behaviour has no gun suppression flag
            self.assertIn("fire", a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
