"""Ghost-v3: a resource-and-survival bot for Builders Mech Royale (protocol mechfight/1).

v3 = v2 plus (every item a CFG flag, v2 behaviour recoverable by switching them off): weapon-specific aiming (gun
intercept vs hitscan laser/lightning), torso-slew-aware alignment, trust of the observation list over the 2D line of
sight, gun-bullet dodging, farm-effectiveness tracking, planner-based approach for stalled resource targets,
upgrade profiles, regen-friendly retreat, threat-aware hide scoring, an expected-value engagement model and a few
lightweight experiments (360 scan, quiet/threat-aware farming, zone-edge defence).

v2 notes:

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
    # ---- v3 priority 1: combat corrections
    AIM_SPLIT=True,      # gun: projectile intercept; laser/lightning: hitscan aim (no flight-time lead)
    TURN_AWARE=True,     # alignment/lead account for the torso slew rate (turn_rate, pitch_rate)
    LAT=0.05,            # seconds between the decision and the first tick the trigger is held
    LASER_HOLD=True,     # keep charging until aligned instead of releasing a misaimed beam
    TRUST_OBS=True,      # an enemy listed in obs.mechs inside our sight region is treated as visible even if the 2D map blocks
    DODGE_GUN=True,      # sidestep visible incoming gun bullets when a lateral move can still get us out of the way
    FARM_EFF=True,       # abandon / reposition when sustained fire does not reduce a crate or drone's HP
    FARM_PLAN=True,      # use the Dijkstra planner to reach a resource target when direct steering stalls
    # ---- v3 priority 3: regeneration
    UPG="damage",        # upgrade profile: damage | regen | escape | weapon
    QUIET_RETREAT=True,  # while retreating do not trade shots unless the target is about to die (keeps the 5 s regen clock free)
    QUIET_GRACE=2.0,     # ... but only once nothing has hit us for this many seconds (while we are being shot, regen cannot run: shoot back)
    # ---- v3 priority 2: threat-aware positioning (planner)
    THREAT_HIDE=False,   # hide-cell scoring uses recently seen enemies (reach time, sight lines, attackers, exits); OFF: -0.4 points in the sim (96 seeds)
    THREAT_MEM=25.0,     # seconds an enemy sighting keeps influencing position scoring
    THREAT_SOUND_W=0.5,  # weight of a sound fix relative to a sighting in position scoring (0 = ignore sounds)
    ESC_W=1.0,           # weight of the escape-route / encirclement terms
    SECT_W=1.0,          # weight of the simultaneous-attacker (open sector) term
    EDGE_W=0.0,          # >0 prefers the outer ring of the next circle (experiment: zone-edge defence)
    CHOKE_W=0.0,         # >0 prefers corridor cells (2-3 open directions) when several hunters are around (experiment)
    REHIDE_S=5.0,        # minimum seconds between threat-driven re-plans of the hiding spot
    # ---- v3 priority 4: selective elimination
    ENGAGE="ev",         # "off" (v2) | "ev" (expected-value model decides optional fights); ON: +0.5 points in the sim (96 seeds)
    EV_MIN=0.0,          # minimum expected placement points for an optional fight (0.0 beat 0.35 and 0.7 in the sim; -0.3 was no better)
    DEATH_COST=2.0,      # points we expect to lose by dying now versus staying alive (a tuning knob, not a measurement)
    EV_RANGE=110.0,
    EV_RECOVER_GATE=True,   # the EV model starts no optional fight while we are regenerating (HP < 85% inside the recovery window); identical to False in the 96-seed sim, kept on so regeneration is not interrupted
    # ---- v3 priority 5: lightweight experiments
    SCAN360=False,       # sweep the full circle while hiding (the 120 degree sight wedge then covers every bearing)
    QUIET_FARM=False,    # no gunfire farming while hunters are suspected nearby (heard / seen recently); OFF: fewer cubes, -0.45 in the sim
    THREAT_FARM=False,   # do not walk to loot or crates that are near a recently seen enemy; OFF (tested together with QUIET_FARM)
)
# One line to get Ghost-v2 back: set PRESET = "v2" (every v3 change off; the decisions are then identical to
# baseline/ghost_bot_v2.py, which tests/check_equivalence.py verifies on recorded sim seeds).
V2_COMPAT = dict(AIM_SPLIT=False, TURN_AWARE=False, LASER_HOLD=False, TRUST_OBS=False, DODGE_GUN=False, FARM_EFF=False,
                 FARM_PLAN=False, UPG="damage", QUIET_RETREAT=False, THREAT_HIDE=False, ENGAGE="off", SCAN360=False,
                 QUIET_FARM=False, THREAT_FARM=False, EDGE_W=0.0, CHOKE_W=0.0)
PRESET = "v3"
if PRESET == "v2":
    CFG.update(V2_COMPAT)
UPGRADE_ORDER = ["gun_damage", "plating", "armor", "gun_rate", "nanites", "laser_damage", "laser_charge",
                 "lightning_efficiency", "gyros", "sensors", "gun_mag", "servos", "gun_velocity", "lightning_range",
                 "lightning_chain", "reactor", "capacitor", "jets", "laser_pierce"]
UPG_PROFILES = {
    "damage": UPGRADE_ORDER,
    # early regeneration: two nanite stacks (+160% regen, regen starts 3 s sooner) before damage
    "regen": ["nanites", "plating", "armor", "gun_damage", "nanites", "gun_rate", "laser_damage", "lightning_efficiency",
              "laser_charge", "gyros", "sensors", "gun_mag", "servos", "gun_velocity", "lightning_range", "lightning_chain",
              "reactor", "capacitor", "jets", "laser_pierce"],
    # movement / escape: speed, cheaper boost, faster turning, then survivability
    "escape": ["servos", "jets", "gyros", "nanites", "plating", "armor", "servos", "gun_damage", "gun_rate", "sensors",
               "capacitor", "reactor", "laser_damage", "lightning_efficiency", "gun_mag", "gun_velocity", "lightning_range",
               "lightning_chain", "laser_charge", "laser_pierce"],
    # weapon-specific: resolved at pick time from the second slot (see Ghost._pick_offer)
    "weapon": None,
}
WEAPON_ORDERS = {
    "lightning": ["lightning_efficiency", "gun_damage", "lightning_range", "lightning_chain", "plating", "armor", "nanites",
                  "capacitor", "reactor", "gun_rate", "gyros", "sensors", "servos", "gun_mag", "gun_velocity", "jets"],
    "laser": ["laser_damage", "laser_charge", "gun_damage", "plating", "armor", "laser_pierce", "nanites", "gyros", "gun_rate",
              "sensors", "servos", "gun_mag", "gun_velocity", "capacitor", "reactor", "jets"],
    "gun": ["gun_damage", "gun_rate", "gun_mag", "plating", "armor", "gun_velocity", "nanites", "gyros", "sensors", "servos",
            "capacitor", "reactor", "jets"],
}


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
    name = "ghost-v3"

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
        self.threats_now = []     # latest threat list (for planner snapshots)
        self.hide_cur = None      # current hiding spot (hysteresis for threat-driven re-plans)
        self.ft = None            # current farm target {"id","best","since","kind","shooting"}
        # ---- v3 state
        self.tracks = {}          # enemy id -> last sighting {"pos","t","hp","w","vel"} (obs keeps real positions 5 s after view)
        self.me_last = None       # latest own state (for upgrade picks)
        self.sound_log = deque(maxlen=60)   # (t, kind, bearing, dist band)
        self.laser_full_t = None  # time the current laser charge reached full
        self.rehide_t = -99.0     # last threat-driven hide re-plan request
        self.hide_info = None     # {"H": (x, y), "cost": float} of the adopted hide plan
        self.threat_sig = None    # signature of the threat set used for the last hide request
        self.scan_yaw = 0.0
        self.scan_t = -99.0
        self.vis_near = 45.0
        self.vis_range = 200.0
        self.vis_fov = math.radians(120.0)
        self.stat = {"gun_dodge": 0, "farm_abandon": 0, "farm_repos": 0, "laser_shots": 0, "laser_hold": 0, "farm_plan": 0,
                     "ev_engage": 0, "ev_skip": 0, "rehide": 0, "quiet_retreat": 0}
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

    def _threat_snapshot(self):
        """Compact copy of the recent enemy picture for the planner thread: (x, y, age, weapons, weight)."""
        if not CFG["THREAT_HIDE"]:
            return ()
        out = []
        for th in self.threats_now[:8]:
            if th["age"] > CFG["THREAT_MEM"] or (th["src"] == "sound" and th["d"] > 200.0):
                continue
            wt = 1.0 if th["src"] == "seen" else CFG["THREAT_SOUND_W"]
            if wt <= 0.0:
                continue
            if th["src"] == "seen" and th["age"] > 12.0:
                wt = 0.6                                   # an old sighting: they have probably moved on
            out.append((th["x"], th["y"], th["age"], tuple(th["w"]) if th["w"] else (), wt))
        return tuple(out)

    def _issue(self, tmpl, x, y, want, keep=False):
        req = dict(tmpl)
        req["pos"] = (x, y)
        req["want"] = want
        req["zone"] = self._zplan
        if req.get("kind") == "hide":
            req["threats"] = self._threat_snapshot()
            req["cur"] = self.hide_cur
        self._request(req, keep)

    def _rehide_check(self, t, x, y, threats, in_zone):
        """Threat-driven re-plan of the hiding spot: when the enemy picture changes materially (a new hunter, or one that
        moved >30 m) and REHIDE_S seconds have passed, ask the planner again, keeping the old plan until the new one lands.
        Costs a signature comparison per tick; the heavy evaluation is the worker's."""
        if not CFG["THREAT_HIDE"] or self.nav is None or self.want is None or self.want[0] != "hide" or self.tmpl is None:
            return
        if t - self.rehide_t < CFG["REHIDE_S"] or t - self.req_t < 1.5:
            return
        rel = [th for th in threats if th["age"] < 15.0 and th["d"] < 170.0]
        sig = frozenset((int(th["x"] // 30), int(th["y"] // 30)) for th in rel[:4])
        if not sig:
            self.threat_sig = None
            return
        if self.threat_sig is not None and len(sig & self.threat_sig) >= len(sig):
            return
        self.threat_sig = sig
        self.rehide_t = t
        self.stat["rehide"] += 1
        self._issue(self.tmpl, x, y, self.want, True)

    def _replan(self, t, x, y):
        """Re-plan the current want from where we are (keeps following the old plan meanwhile).
        Skipped while a recent request is still in flight so results are never starved."""
        if self.tmpl is not None and self.nav is not None and t - self.req_t > 1.5:
            self._issue(self.tmpl, x, y, self.want, True)

    def _select_hide(self, dist, ncx, ncy, nr, label, sizes, amax, threats=None, cur=None):
        """Pick the hiding cell inside the next circle (low exposure, short trip, biggest connected region).

        With `threats` (recent enemy sightings / sound fixes) the best K cells by the static score are re-ranked with
        threat terms: enemy reach time vs our travel time, sight lines from the enemy, simultaneous attackers (occupied
        bearing sectors), and whether an escape direction away from them exists. Heavy work stays in the planner thread;
        the 50 ms cycle only compares cached signatures (see Ghost._rehide_check)."""
        nav = self.nav
        nx = nav.nx
        INF = 1e17
        rc = max(6.0, min(0.8 * nr, nr - 12.0))
        i0, j0 = nav.cell_of(ncx - rc, ncy - rc)
        i1, j1 = nav.cell_of(ncx + rc, ncy + rc)
        stride = 1
        while ((i1 - i0 + 1) * (j1 - j0 + 1)) / (stride * stride) > 2500:
            stride += 1
        edge_w = CFG["EDGE_W"]
        cand = []
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
                cost = e + 0.025 * d * CS + (0.08 - 0.12 * edge_w) * dc
                if label is not None:
                    cid = label.get(k)
                    cost += 100.0 * (1.0 - sizes[cid] / amax) if cid is not None else 100.0
                cand.append((cost, k, cx, cy, a, e, d))
        best = None
        if cand:
            cand.sort(key=lambda c: c[0])
            if threats:
                K = cand[:40]
                if cur is not None:
                    for c in cand[40:]:
                        if math.hypot(c[2] - cur[0], c[3] - cur[1]) < 6.0:
                            K.append(c)
                            break
                scored = []
                for c in K:
                    pen = self._threat_pen(c[2], c[3], c[6], threats)
                    if cur is not None and math.hypot(c[2] - cur[0], c[3] - cur[1]) < 6.0:
                        pen -= 8.0                  # hysteresis: do not abandon the current spot for a marginal gain
                    scored.append((c[0] + pen, c))
                scored.sort(key=lambda sc: sc[0])
                c = scored[0][1]
            else:
                c = cand[0]
            best = (c[0], c[1], c[2], c[3], c[4], c[5])
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

    def _threat_pen(self, cx, cy, d_cells, threats):
        """Extra cost of hiding at (cx, cy) given recent enemy information. Units match the static score (metres of
        open sight distance): a fully exposed, contested cell adds roughly 60-120."""
        nav = self.nav
        t_me = d_cells * CS / 11.7
        pen = 0.0
        sectors = set()
        near = 0
        bearings = []
        for th in threats:
            wt = th[4]                                      # 1.0 for a sighting, ~0.5 for a sound fix
            r = math.hypot(th[0] - cx, th[1] - cy)
            age = th[2]
            tr = max(0.0, r - 20.0) / 11.7                  # time for that enemy to be on us
            pen += 25.0 * wt * max(0.0, min(1.5, (t_me + 2.0 - tr) / 6.0))
            pen += 45.0 * wt * max(0.0, 1.0 - r / 100.0)
            if r < 190.0 and (r < 45.0 or nav.los(th[0], th[1], cx, cy)):
                pen += 22.0 * wt * (1.0 - r / 220.0)
            if r < 140.0:
                brg = math.atan2(th[1] - cy, th[0] - cx)
                bearings.append(brg)
                sectors.add(int(((brg + TAU) % TAU) / (TAU / 4.0)))
            if r < 110.0:
                near += 1
        if len(sectors) > 1:
            pen += 18.0 * CFG["SECT_W"] * (len(sectors) - 1)
        if near > 1:
            pen += 10.0 * CFG["SECT_W"] * (near - 1)
        if bearings:
            exits = 0
            opens = 0
            for q in range(8):
                a = TAU * q / 8.0
                if nav.ray_len(cx, cy, a, 40.0) >= 30.0:
                    opens += 1
                    if all(abs(angdiff(a, b)) > 1.2 for b in bearings):
                        exits += 1
            if exits == 0:
                pen += 35.0 * CFG["ESC_W"]
            elif exits == 1:
                pen += 12.0 * CFG["ESC_W"]
            if CFG["CHOKE_W"] > 0.0 and len(bearings) >= 2 and exits >= 1 and 2 <= opens <= 3:
                pen -= 15.0 * CFG["CHOKE_W"]         # a corridor forces pursuers into single file
        return pen

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
            best = self._select_hide(dist, ncx, ncy, nr, label, sizes, amax, req.get("threats"), req.get("cur"))
            if best is None:
                return None
            _, k, hx, hy, ang, expo = best
            poly = self._poly(par, k, x, y, hx, hy)
            if zc and zp[s[1] * nx + s[0]] <= 0.0 and self._outside_len(poly, zc) > 12.0:
                # the cheapest route leaves the zone for a long stretch (barrier inside the circle): stay inside
                # and wait at the reachable cell closest to the target instead
                zph = [1e18 if v > 0.0 else 0.0 for v in zp]
                dist2, par2 = nav.dijkstra(s[0], s[1], zph)
                b2 = self._select_hide(dist2, ncx, ncy, nr, label, sizes, amax, req.get("threats"), req.get("cur"))
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
        order = UPG_PROFILES.get(CFG["UPG"], UPGRADE_ORDER)
        me = self.me_last
        if order is None:                                   # weapon-specific profile: follow the second slot
            w1 = None
            if me and len(me.get("weapons") or []) > 1 and me["weapons"][1]:
                w1 = me["weapons"][1].get("kind")
            order = WEAPON_ORDERS.get(w1, WEAPON_ORDERS["gun"])
        owned = {}
        for u in (me.get("upgrades") or []) if me else []:
            uid = u.get("id") if isinstance(u, dict) else u
            owned[uid] = owned.get(uid, 0) + 1
        best, bi = 10 ** 6, 0
        for i, ch in enumerate(offer.get("choices", [])):
            cid = ch.get("id", "")
            occ = [k for k, o in enumerate(order) if o == cid]
            n = owned.get(cid, 0)
            if not occ:
                r = 500
            elif n < len(occ):
                r = occ[n]                                  # the n-th stack uses the n-th listed position
            else:
                r = occ[-1] + 0.5
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
        if CFG["DODGE_GUN"]:
            g = self._gun_dodge(obs, me)
            if g is not None:
                gx += g[0]
                gy += g[1]
                urgent = urgent or g[2]
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

    def _threats(self, t, x, y):
        """Recently seen enemy mechs (exact positions: the server tracks them for 5 s after they leave view, and our own
        memory keeps the last sighting for THREAT_MEM seconds) plus coarse positions from heard sounds.
        Entries: {"x","y","age","w","src","d"}; cheap, no map access."""
        out = []
        mem = CFG["THREAT_MEM"]
        for k, v in self.tracks.items():
            age = t - v["t"]
            if age > mem:
                continue
            ex = v["pos"][0] + v["vel"][0] * min(age, 1.5)
            ey = v["pos"][1] + v["vel"][1] * min(age, 1.5)
            out.append({"x": ex, "y": ey, "age": age, "w": v["w"], "src": "seen", "d": math.hypot(ex - x, ey - y), "id": k})
        band = {"near": 35.0, "mid": 110.0, "far": 220.0}
        for (ts_, kind, brg, dist) in list(self.sound_log):
            age = t - ts_
            if age > 10.0:
                continue
            r = band.get(dist, 150.0)
            sx, sy = x + math.cos(brg) * r, y + math.sin(brg) * r
            if any(o["src"] == "seen" and o["age"] < 12.0 and math.hypot(o["x"] - sx, o["y"] - sy) < 70.0 for o in out):
                continue
            w = [kind] if kind in ("laser", "laser_charge", "lightning", "mortar") else ["gun"]
            out.append({"x": sx, "y": sy, "age": age, "w": w, "src": "sound", "d": r, "id": None})
            if len(out) > 20:
                break
        out.sort(key=lambda o: o["d"])
        return out

    def _gun_dodge(self, obs, me):
        """Sidestep visible enemy gun bullets that will hit us unless we move, when a lateral step can still clear them.

        A bullet crosses 252 m/s; our ground acceleration is 58.5 m/s^2 up to 11.7 m/s, so a 3 m sidestep needs about 0.35 s,
        i.e. a bullet more than ~90 m away. Bullets closer than that cannot be dodged and are ignored (no wasted movement).
        Returns (dx, dy, urgent) or None."""
        projs = obs.get("projectiles") or []
        if not projs:
            return None
        x, y, z = me["pos"]
        cubes = me.get("cubes", 0) or 0
        R = 2.5 * (1.0 + 0.05 * cubes) + 0.3
        height = 9.0 * (1.0 + 0.05 * cubes)
        vel = me.get("vel") or [0.0, 0.0, 0.0]
        ux_, uy_ = fin(vel[0]), fin(vel[1])
        gx = gy = 0.0
        urgent = False
        found = False
        for p in projs:
            if p.get("kind") != "gun":
                continue
            px, py, pz = p["pos"][0], p["pos"][1], zof(p["pos"], z + 4.5)
            vx, vy = p["vel"][0], p["vel"][1]
            vz = p["vel"][2] if len(p["vel"]) > 2 else 0.0
            wx, wy = vx - ux_, vy - uy_
            w2 = wx * wx + wy * wy
            if w2 < 1.0:
                continue
            rx, ry = px - x, py - y
            ts = -(rx * wx + ry * wy) / w2
            if ts <= 0.0 or ts > 1.0:
                continue
            ox, oy = rx + wx * ts, ry + wy * ts           # bullet minus us at closest approach (horizontal)
            dh = math.hypot(ox, oy)
            zb = pz + vz * ts
            if dh >= R or zb < z - 0.5 or zb > z + height + 0.5:
                continue                                    # it misses anyway
            tau = ts - 0.1                                  # the move only starts with the next decision
            if tau <= 0.0:
                continue
            reach = 0.5 * 58.5 * tau * tau if tau <= 0.2 else 1.17 + 11.7 * (tau - 0.2)
            if reach < (R - dh) + 0.3:
                continue                                    # cannot get clear in time: do not bother
            if dh < 0.05:                                   # dead centre: pick a side perpendicular to the flight line
                nx_, ny_ = -wy, wx
            else:
                nx_, ny_ = -ox, -oy                         # move away from the bullet's path
            nx_, ny_ = norm2(nx_, ny_)
            wgt = 1.0 / max(ts, 0.3)
            gx += nx_ * wgt
            gy += ny_ * wgt
            urgent = urgent or ts < 0.6
            found = True
        if not found:
            return None
        self.stat["gun_dodge"] += 1
        ux, uy = norm2(gx, gy)
        return ux * 1.5, uy * 1.5, urgent

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
        self.me_last = me
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
            if sd.get("kind") in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar", "explosion") \
                    and sd.get("age", 9.0) < 0.8:
                self.sound_log.append((t - sd.get("age", 0.0), sd["kind"], fin(sd.get("bearing")), sd.get("dist", "far")))

        # ---- adopt a finished plan
        pn = self.plan_new
        if pn is not None:
            self.plan_new = None
            if pn["ver"] == self.ver or (self.want is not None and pn.get("want") == self.want):
                self.plan = pn
                self.pk = 0
                if pn.get("kind") == "hide":
                    self.hide_cur = pn["H"]

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
        st_me = me.get("stats") or {}
        self.vis_near = 45.0 + fin(st_me.get("near_range_bonus"), 0.0)
        self.vis_range = 200.0 + fin(st_me.get("vision_bonus"), 0.0)
        ay0 = me["aim"][0]
        for m in enemies + npcs:
            if m["_dh"] < 160:
                seen = nav.los(x, y, m["pos"][0], m["pos"][1]) if nav else True
                if not seen and CFG["TRUST_OBS"]:
                    # the server lists it, our 2D raster says "blocked": inside our real sight region the server wins
                    brg = math.atan2(m["pos"][1] - y, m["pos"][0] - x)
                    seen = m["_dh"] <= self.vis_near or abs(angdiff(brg, ay0)) <= self.vis_fov / 2
                m["_vis"] = seen
            else:
                m["_vis"] = False
        enemies.sort(key=lambda m: m["_d"])
        npcs.sort(key=lambda m: m["_d"])
        for m in enemies:
            vv = m.get("vel") or [0.0, 0.0, 0.0]
            self.tracks[m["id"]] = {"pos": (m["pos"][0], m["pos"][1]), "t": t, "hp": m.get("hp"), "w": m.get("weapons") or [],
                                    "vel": (fin(vv[0]), fin(vv[1])), "vis": m["_vis"]}
        if len(self.tracks) > 24:
            for k in [k for k, v in self.tracks.items() if t - v["t"] > CFG["THREAT_MEM"]]:
                del self.tracks[k]
        threats = self._threats(t, x, y)
        self.threats_now = threats

        # ---- what do we want to be doing (plan request)
        key = (round(nc[0]), round(nc[1]), round(nr))
        drop = self._drop_target(obs, me, enemies, nc, nr, walk)
        fgoal = None
        if CFG["FARM_PLAN"] and not drop and self.ft is not None and self.ft.get("plan") and self.ft.get("goal"):
            fgoal = self.ft["goal"]
        if drop:
            want = ("drop", drop["id"])
        elif fgoal is not None:
            want = ("farm", self.ft["id"], round(fgoal[0]), round(fgoal[1]))
        else:
            want = ("hide", key)
        if nav is None:
            self.want = None          # keep asking until the map is loaded
        elif want != self.want:
            self.want = want
            if drop:
                self.tmpl = {"kind": "point", "goal": (drop["pos"][0], drop["pos"][1])}
            elif fgoal is not None:
                self.tmpl = {"kind": "point", "goal": (fgoal[0], fgoal[1])}
            else:
                self.tmpl = {"kind": "hide", "nc": (nc[0], nc[1]), "nr": nr}
            self._issue(self.tmpl, x, y, want, False)

        self._rehide_check(t, x, y, threats, in_zone)

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
                plan_dir = None
                if self.want is not None and self.want[0] == "farm" and self.plan is not None \
                        and self.plan.get("want") == self.want:
                    plan_dir = (gdx, gdy)          # path-following direction toward the farm stand point
                farm = self._farm(obs, me, npcs, nc, nr, t, x, y, threats, plan_dir)
            if farm is None:
                loot_active, ldx, ldy = self._loot(obs, me, nc, nr, drop, t, x, y, threats)
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
            elif CFG["SCAN360"] and not (gdx or gdy) and farm is None and not loot_active and calm:
                # standing still and calm: sweep the 120 degree / 200 m sight wedge around the full circle (~4 s per turn)
                dts = t - self.scan_t
                if dts > 0.35:                         # a fresh sweep starts from where the torso already points
                    self.scan_yaw, dts = me["aim"][0], 0.1
                self.scan_yaw = math.atan2(math.sin(self.scan_yaw + 1.6 * min(0.2, dts)), math.cos(self.scan_yaw + 1.6 * min(0.2, dts)))
                self.scan_t = t
                aim_yaw = self.scan_yaw
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
    def _lead(self, me, tgt, speed, extra=0.0):
        """Aim solution (yaw, pitch, distance) for a target. speed=None means hitscan (no flight time); `extra` seconds
        of target motion are added to the horizon (decision latency, torso slew)."""
        mx, my, mz = me["pos"]
        mh = me.get("stats", {}).get("muzzle_height", 7.0)
        tx, ty, tz = tgt["pos"]
        vel = tgt.get("vel") or [0, 0, 0]
        vx, vy = fin(vel[0]), fin(vel[1])
        if tgt["kind"] == "mech":
            tc = tz + 4.5 * (1 + 0.05 * (tgt.get("cubes", 0) or 0))
        else:
            tc = tz + 1.75
        oz = mz + mh
        d = math.sqrt((tx - mx) ** 2 + (ty - my) ** 2 + (tc - oz) ** 2)
        px, py, pz = tx, ty, tc
        for _ in range(3):
            tof = min(1.0, d / max(speed, 1.0)) if speed else 0.0
            th = tof + extra
            px, py, pz = tx + vx * th, ty + vy * th, tc
            d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - oz) ** 2)
        yaw = math.atan2(py - my, px - mx)
        pitch = math.atan2(pz - oz, math.hypot(px - mx, py - my))
        return yaw, pitch, d

    @staticmethod
    def _rates(me):
        st = me.get("stats") or {}
        return max(0.5, fin(st.get("turn_rate"), 4.0)), max(0.5, fin(st.get("pitch_rate"), 3.0))

    def _slew_time(self, me, dyaw, dpitch):
        """Seconds the torso needs to rotate by (dyaw, dpitch) at its actual rates (gyro upgrades included)."""
        tr, pr = self._rates(me)
        return min(0.6, max(abs(dyaw) / tr, abs(dpitch) / pr))

    def _aim_for(self, me, tgt, kind, speed=252.0):
        """Weapon-specific firing solution. gun: projectile intercept; laser / lightning: hitscan, so no flight-time lead.
        With TURN_AWARE the horizon also covers the decision latency and the time the torso needs to get on target."""
        if not CFG["AIM_SPLIT"]:
            return self._lead(me, tgt, speed)
        sp = speed if kind == "gun" else None
        if not CFG["TURN_AWARE"]:
            return self._lead(me, tgt, sp)
        yaw, pitch, d = self._lead(me, tgt, sp, CFG["LAT"])
        ayaw, apit = me["aim"]
        st = min(0.3, self._slew_time(me, angdiff(yaw, ayaw), pitch - apit))
        return self._lead(me, tgt, sp, CFG["LAT"] + st)

    def _aligned(self, me, yaw, pitch, tol_y, tol_p):
        """Is the torso on the solution at the moment the shot leaves? The shot leaves during the next 2 ticks, over which the
        torso slews at most rate*dt toward the commanded aim; we grant half a tick of that slew, never more than half the tolerance."""
        ayaw, apit = me["aim"]
        ey = abs(angdiff(yaw, ayaw))
        ep = abs(pitch - apit)
        if CFG["TURN_AWARE"]:
            tr, pr = self._rates(me)
            ey = max(0.0, ey - min(tr * 0.025, 0.5 * tol_y))
            ep = max(0.0, ep - min(pr * 0.025, 0.5 * tol_p))
        return ey < tol_y and ep < tol_p

    def _tracking_ok(self, me, tgt, d):
        """Can the torso follow the target's angular motion at all? (lateral speed / distance vs the turn rate)"""
        if not CFG["TURN_AWARE"]:
            return True
        vel = tgt.get("vel") or [0.0, 0.0, 0.0]
        dx, dy = tgt["pos"][0] - me["pos"][0], tgt["pos"][1] - me["pos"][1]
        n = math.hypot(dx, dy)
        if n < 1.0:
            return False
        vperp = abs((-dy * fin(vel[0]) + dx * fin(vel[1])) / n)
        return vperp / max(n, 1.0) < 0.85 * self._rates(me)[0]

    def _laser_fire(self, wp, st, dh, aligned_start, aligned_release, crowd, abort, t):
        """Laser trigger: True = hold (charge), False = release (the beam fires along the aim at that moment)."""
        ch = fin(wp.get("charge"), 0.0)
        full = st.get("charge", 0.75)
        if ch <= 0.0:
            self.laser_full_t = None
            return bool(wp.get("cooldown", 0) <= 0.05 and dh <= st.get("range", 280) * 0.9 and aligned_start
                        and crowd == 0 and dh > 20 and not abort)
        if not CFG["LASER_HOLD"]:
            return ch < full - 0.05 and aligned_release               # v2: release at full charge, whatever the aim
        if abort:
            return False
        if ch < full - 0.04:
            self.stat["laser_hold"] += 1
            return True                                               # still charging: keep tracking
        if self.laser_full_t is None:
            self.laser_full_t = t
        if aligned_release or t - self.laser_full_t > 0.45:           # on target now, or do not sit half-speed any longer
            self.stat["laser_shots"] += 1
            return False
        return True

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
        recovering = CFG["QUIET_RETREAT"] and t < self.recover_until and frac < 0.85
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
            if CFG["ENGAGE"] == "ev" and e["kind"] == "mech" and (weak or d <= CFG["EV_RANGE"]) and not (provoked or close or keep):
                # optional fight (it is neither provoking us nor close enough to catch us anyway): the EV model decides
                ev = self._ev_engage(me, e, near, vis, t)
                ok = ev >= CFG["EV_MIN"] and frac > 0.5 and not (recovering and CFG["EV_RECOVER_GATE"])
                if ok and not weak:
                    self.stat["ev_engage"] += 1
                elif weak and not ok:
                    self.stat["ev_skip"] += 1
                opp, weak = ok, False
            elif recovering and not (provoked or close or keep):
                opp = weak = False                   # regenerating: no new engagements (they restart the 5 s clock)
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

    def _has_exit(self, x, y, bearings):
        """Is there a walkable direction (free 20 m ray) pointing away from every listed enemy bearing?"""
        nav = self.nav
        if nav is None:
            return True
        for q in range(8):
            a = TAU * q / 8.0
            if all(abs(angdiff(a, b)) > 1.0 for b in bearings) and nav.free_ray(x, y, math.cos(a), math.sin(a), 20.0):
                return True
        return False

    def _ev_engage(self, me, e, near, vis, t):
        """Expected placement points of an OPTIONAL fight against mech `e` (positive = take it).

        p(win) from time-to-kill vs time-to-die, with our slot-1 weapon against theirs (laser > mortar > lightning > laser),
        lightning chaining through clusters, every hunter around the target (visible or seen <6 s ago) added to the damage
        we will take, and a missing escape direction multiplying the cost of losing. Gain/cost scales are the DEATH_COST and
        gain knobs in CFG, not measurements."""
        d = e["_dh"]
        ehp = e.get("hp") or 1400.0
        ws = me.get("weapons") or []
        k1 = ws[1].get("kind") if len(ws) > 1 and ws[1] else None
        beats = {"laser": "mortar", "mortar": "lightning", "lightning": "laser"}
        mult = 1.0
        for ewk in (e.get("weapons") or []):
            if k1 and beats.get(k1) == ewk:
                mult = max(mult, 1.25)
            elif k1 and beats.get(ewk) == k1:
                mult = min(mult, 0.8)
        ex, ey = e["pos"][0], e["pos"][1]
        if k1 == "lightning" and d < 42.0:
            ncl = sum(1 for o in vis if o is not e and math.hypot(o["pos"][0] - ex, o["pos"][1] - ey) < 14.0)
            mult *= 1.0 + 0.45 * min(2, ncl)
        tk = ehp / (self._my_dps(me, d) * mult)
        D = sum(self._enemy_dps(o) for o in near)
        seen_ids = {o["id"] for o in vis}
        hunters = 0
        for k, tr in self.tracks.items():
            if k == e["id"] or k in seen_ids or t - tr["t"] > 6.0:
                continue
            if math.hypot(tr["pos"][0] - ex, tr["pos"][1] - ey) < 110.0:
                hunters += 1
                D += 0.6 * 187.0 * 0.4
        hunters += sum(1 for o in vis if o is not e and math.hypot(o["pos"][0] - ex, o["pos"][1] - ey) < 110.0)
        td = me["hp"] / max(D, 60.0)
        p = 1.0 / (1.0 + (tk / max(td, 0.1)) ** 3)
        isolated = hunters == 0 and len(near) <= 1
        gain = 1.0 + (0.3 if (isolated and k1 == "laser") else 0.0)
        x, y = me["pos"][0], me["pos"][1]
        brg = [math.atan2(o["pos"][1] - y, o["pos"][0] - x) for o in near] or [math.atan2(ey - y, ex - x)]
        cost = CFG["DEATH_COST"] * (1.0 + 0.25 * hunters) * (1.0 if self._has_exit(x, y, brg) else 1.3)
        return p * gain - (1.0 - p) * cost

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
        w0 = w[0] if w else None
        w1 = w[1] if len(w) > 1 else None
        speed = 252.0
        if w0 and w0.get("kind") == "gun":
            speed = w0["stats"].get("speed", 252.0)
        dh = tgt["_dh"]
        crowd = sum(1 for e in enemies if e["_vis"] and e["_dh"] < 40)
        flee = cmode == "flee"
        # which solution does the torso follow? A charging / ready laser needs its exact (zero-lead) line; otherwise the gun's.
        laser_slot = 1 if (w1 and w1.get("kind") == "laser") else None
        laser_on = False
        if laser_slot is not None:
            lst = w1.get("stats", {})
            laser_on = fin(w1.get("charge")) > 0.0 or (w1.get("cooldown", 0) <= 0.05 and 20 < dh <= lst.get("range", 280) * 0.9
                                                       and crowd == 0)
        if CFG["AIM_SPLIT"]:
            yaw, pitch, d = self._aim_for(me, tgt, "laser" if laser_on else "gun", speed)
        else:
            yaw, pitch, d = self._lead(me, tgt, speed)
        pitch = max(self.min_pitch, min(self.max_pitch, pitch))
        ayaw, apit = me["aim"]
        yerr = abs(angdiff(ayaw, yaw))
        tol = max(0.025, min(0.12, math.atan2(2.2, max(dh, 1.0))))
        aligned = self._aligned(me, yaw, pitch, tol, tol * 1.6)
        if CFG["AIM_SPLIT"] and laser_on:
            # the torso follows the laser's zero-lead line; the gun may shoot along it only where its own (leading)
            # solution agrees with that line (a still target), otherwise the bullets would miss and waste the magazine
            gy_, gp_, _ = self._aim_for(me, tgt, "gun", speed)
            aligned = self._aligned(me, gy_, max(self.min_pitch, min(self.max_pitch, gp_)), tol, tol * 1.6)
        track_ok = self._tracking_ok(me, tgt, dh)
        fire = [False, False]
        for slot in (0, 1):
            wp = w[slot] if slot < len(w) else None
            if not wp:
                continue
            kind = wp.get("kind")
            st = wp.get("stats", {})
            if kind == "gun":
                if wp.get("ammo", 1) > 0 and wp.get("reload", 0) <= 0 and dh <= st.get("range", 230) * 0.85 and aligned \
                        and track_ok:
                    fire[slot] = True
            elif kind == "laser":
                a_start = self._aligned(me, yaw, pitch, tol * 2.5, tol * 4.0) if CFG["TURN_AWARE"] else yerr < tol * 2.5
                a_rel = self._aligned(me, yaw, pitch, tol * 1.2, tol * 2.0) if CFG["TURN_AWARE"] else yerr < tol * 3
                abort = flee and CFG["QUIET_RETREAT"] and t - self.last_damage_t > CFG["QUIET_GRACE"]
                fire[slot] = self._laser_fire(wp, st, dh, a_start, a_rel, crowd, abort, t)
            elif kind == "lightning":
                cone = math.radians(st.get("cone_deg", 30.0)) / 2
                if CFG["AIM_SPLIT"]:
                    ly, _, _ = self._lead(me, tgt, None, 0.0)               # hitscan: bearing to where it is now
                    lerr = abs(angdiff(ayaw, ly))
                else:
                    lerr = yerr
                if dh <= st.get("range", 45) * 0.92 and lerr < cone * 0.6 and me.get("energy", 0) >= 6:
                    fire[slot] = True
        if flee and CFG["QUIET_RETREAT"] and t - self.last_damage_t > CFG["QUIET_GRACE"]:
            # nothing is hitting us now: shooting would reset the 5 s regeneration clock and invite fire back, so only take
            # a shot that finishes the target (while we are under fire regeneration cannot run anyway: shoot back)
            if (tgt.get("hp") or 1400.0) > 110.0 and fire != [False, False]:
                fire = [False, False]
                self.stat["quiet_retreat"] += 1
        # ---- movement: hold the preferred range, strafe unpredictably, stay inside the zone, keep clear of walls
        ex, ey = tgt["pos"][0] - x, tgt["pos"][1] - y
        ux, uy = norm2(ex, ey)
        if t > self.strafe_until:
            self.strafe_sign = self.rng.choice((-1, 1))
            self.strafe_until = t + self.rng.uniform(0.6, 1.5)
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
    def _stand_point(self, tpos, x, y, nc, nr, z, rads=(22.0, 15.0, 9.0)):
        """A free cell 9-22 m from the target with (2D) line of sight to it, inside the next circle, nearest to us."""
        nav = self.nav
        if nav is None:
            return None
        best = None
        for r in rads:
            for k in range(16):
                a = TAU * k / 16
                px, py = tpos[0] + math.cos(a) * r, tpos[1] + math.sin(a) * r
                if nav.block_at(px, py) or math.hypot(px - nc[0], py - nc[1]) > nr - 8.0:
                    continue
                if abs(nav.gh(px, py) - z) > 7.0 or not nav.los(px, py, tpos[0], tpos[1]):
                    continue
                dd = math.hypot(px - x, py - y) + 0.3 * r
                if best is None or dd < best[0]:
                    best = (dd, px, py)
        return (best[1], best[2]) if best else None

    def _near_threat(self, threats, px, py, rad, max_age):
        for th in threats or ():
            if th["age"] <= max_age and math.hypot(th["x"] - px, th["y"] - py) < rad:
                return True
        return False

    def _farm(self, obs, me, npcs, nc, nr, t, x, y, threats=None, plan_dir=None):
        """Pick a crate / drone / cube / weapon worth a short detour and return the action for it (or None)."""
        z = me["pos"][2]
        ws = me.get("weapons") or []
        slot1_free = len(ws) > 1 and ws[1] is None
        if me["hp"] < CFG["FARM_HP"] * me["max_hp"]:
            self.ft = None
            return None
        early = t < CFG["FARM_UNTIL"]
        cands = []
        noisy_ok = True
        if CFG["QUIET_FARM"] and threats and any(th["d"] < 170.0 and th["age"] < 10.0 for th in threats):
            noisy_ok = False                  # gunfire carries 320 m; do not announce ourselves while hunters are close
        w1k = (ws[1].get("kind") if len(ws) > 1 and ws[1] else None)

        def ok_place(p, margin):
            if math.hypot(p[0] - nc[0], p[1] - nc[1]) >= nr - margin or abs(zof(p, z) - z) > 7.0:
                return False
            if CFG["THREAT_FARM"] and self._near_threat(threats, p[0], p[1], 55.0, 15.0):
                return False
            return True

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
        if early and (noisy_ok or w1k == "lightning"):
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
        hp_now = None
        if kind == "crate":
            for c in obs.get("crates", []):
                if c["id"] == cid:
                    hp_now = c.get("hp")
        elif kind == "npc":
            for n in npcs:
                if n["id"] == cid:
                    hp_now = n.get("hp")
        if self.ft is None or self.ft["id"] != cid:
            self.ft = {"id": cid, "best": dh, "since": t, "kind": kind, "plan": False, "goal": None, "fire_s": 0.0,
                       "hp_mark": hp_now, "bad": 0, "repo_until": 0.0, "shooting": False}
        ft = self.ft
        tprev = ft.get("tp", t - 0.1)
        ft["tp"] = t
        # ---- progress / stall handling: first ask the planner for a route, then give up on the target
        stall_limit = 2.5 if (CFG["FARM_PLAN"] and not ft["plan"]) else (6.0 if kind in ("npc", "crate") else 3.5)
        if CFG["FARM_PLAN"] and ft["plan"]:
            stall_limit = 7.0
        if dh < ft["best"] - 1.5:
            ft["best"], ft["since"] = dh, t
        elif t - ft["since"] > stall_limit and not ft.get("shooting") and t >= ft["repo_until"]:
            if CFG["FARM_PLAN"] and not ft["plan"] and self.nav is not None:
                goal = pos if kind in ("pick", "cube") else self._stand_point(pos, x, y, nc, nr, z)
                if goal is not None:
                    ft["plan"], ft["goal"], ft["since"] = True, (goal[0], goal[1]), t
                    self.stat["farm_plan"] += 1
                    return {"mx": 0.0, "my": 0.0, "yaw": math.atan2(pos[1] - y, pos[0] - x), "pitch": 0.0, "fire": [False, False]}
            self.black[cid] = t + 45.0
            self.ft = None
            self.stat["farm_abandon"] += 1
            return None
        ux, uy = norm2(pos[0] - x, pos[1] - y)
        res = {"mx": 0.0, "my": 0.0, "yaw": math.atan2(pos[1] - y, pos[0] - x), "pitch": 0.0, "fire": [False, False]}
        stand = CFG["FARM_STAND"]
        ft["shooting"] = False

        def approach(tx_, ty_, d_):
            if ft["plan"] and plan_dir is not None and (kind in ("npc", "crate") or d_ > 6.0):
                return plan_dir
            sx, sy = self._steer(x, y, *norm2(tx_ - x, ty_ - y), look=6.0)
            return sx, sy

        if kind in ("pick", "cube"):
            if dh > (1.5 if kind == "cube" else 2.6):
                res["mx"], res["my"] = approach(pos[0], pos[1], dh)
            return res
        # npc / crate: close to firing range with a clear line, then shoot until it breaks
        tgt = {"kind": "npc" if kind == "npc" else "crate", "pos": pos, "vel": [0, 0, 0]}
        if kind == "npc":
            for n in npcs:
                if n["id"] == cid:
                    tgt = n
                    break
        yaw, pitch, d = self._aim_for(me, tgt, "gun", 252.0)
        pitch = max(self.min_pitch, min(self.max_pitch, pitch))
        res["yaw"], res["pitch"] = yaw, pitch
        los = self.nav.los(x, y, pos[0], pos[1]) if self.nav is not None else True
        repositioning = t < ft["repo_until"]
        if repositioning or dh > stand or not los:
            gx_, gy_ = (pos[0], pos[1]) if not (ft["plan"] and ft["goal"]) else ft["goal"]
            res["mx"], res["my"] = approach(gx_, gy_, dh)
        w0 = ws[0] if ws else None
        ayaw, apit = me["aim"]
        tol = max(0.03, min(0.12, math.atan2(2.4, max(dh, 1.0))))
        if los and dh <= 62 and not repositioning:
            if w0 and w0.get("kind") == "gun" and w0.get("ammo", 1) > 0 and w0.get("reload", 0) <= 0 \
                    and self._aligned(me, yaw, pitch, tol, tol * 1.6):
                res["fire"][0] = True
                ft["shooting"] = True
            w1 = ws[1] if len(ws) > 1 else None
            if w1 and w1.get("kind") == "lightning" and dh < 38 and abs(angdiff(ayaw, yaw)) < 0.2 and me.get("energy", 0) > 40:
                res["fire"][1] = True
                ft["shooting"] = True
        # ---- is the fire doing anything? (a wall between us and the target, a roof crate, a wrong angle ...)
        if CFG["FARM_EFF"] and hp_now is not None:
            if ft["hp_mark"] is None or hp_now < ft["hp_mark"] - 15.0:
                ft["hp_mark"], ft["fire_s"], ft["bad"] = hp_now, 0.0, 0
            elif ft["shooting"]:
                ft["fire_s"] += min(0.25, max(0.0, t - tprev))
                if ft["fire_s"] >= 1.6:                      # 1.6 s of fire (about 6 gun shots) without 15 HP of damage
                    ft["bad"] += 1
                    ft["fire_s"] = 0.0
                    ft["hp_mark"] = hp_now
                    if ft["bad"] >= 2 or self.nav is None:
                        self.black[cid] = t + 60.0
                        self.ft = None
                        self.stat["farm_abandon"] += 1
                        return None
                    sp = self._stand_point(pos, x, y, nc, nr, z, rads=(16.0, 11.0, 24.0))
                    if sp is None:
                        self.black[cid] = t + 60.0
                        self.ft = None
                        self.stat["farm_abandon"] += 1
                        return None
                    ft["plan"], ft["goal"], ft["repo_until"], ft["since"] = True, (sp[0], sp[1]), t + 4.0, t
                    ft["best"] = 1e9
                    self.stat["farm_repos"] += 1
                    res["fire"] = [False, False]
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

    def _loot(self, obs, me, nc, nr, drop, t, x, y, threats=None):
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
                if CFG["THREAT_FARM"] and self._near_threat(threats, p["pos"][0], p["pos"][1], 55.0, 15.0):
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
                if CFG["THREAT_FARM"] and self._near_threat(threats, c["pos"][0], c["pos"][1], 55.0, 15.0):
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
