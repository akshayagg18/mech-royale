# Ghost-v3: implementation record

Scope (from the brief): implement the supplied research findings, keep v2 recoverable, test, validate, submit. No new research
and no new architecture. This file records what was built, how each change is switched, what the local evidence says, what was
rejected, and what is still unknown.

**Status.** There is no official v3 score. The verified v2 score is **3.31** (commit `f0b1fb308da49c4d79cd563ea8501c252cf47325`);
the minimum target is 6.50. Nothing below claims the target; the local simulator is my own model and is **not** evidence of
an official gain (it is used here to catch regressions and to veto changes that clearly hurt).

## 1. How v2 stays recoverable

* `baseline/ghost_bot_v2.py` is the frozen v2 file (`src/ghost_bot_v2.py` in the workspace). It is byte-identical to the file in commit `f0b1fb3`.
* `PRESET = "v2"` in `ghost_bot.py` turns every v3 change off. `tests/check_equivalence.py` runs the v3 file with those flags and the
  frozen v2 on the same simulator seeds and requires identical per-seed results (the simulator is deterministic).
  Result on seeds 1-12 (sense, diff 2): all 12 identical (points, kills, damage, cubes, survival time).
* Every v3 change is a `CFG` flag (table in section 2), so an official run can change one thing at a time.

## 2. What changed, by priority

### Priority 1 - combat corrections (`AIM_SPLIT`, `TURN_AWARE`, `LASER_HOLD`, `TRUST_OBS`, `DODGE_GUN`, `FARM_EFF`, `FARM_PLAN`)

| # | Finding | Implementation | Flag |
| --- | --- | --- | --- |
| 1 | Gun needs projectile interception; laser and lightning are hitscan | `_lead(..., speed=None)` has no flight time; `_aim_for(kind)` picks gun intercept or hitscan line. While a laser is charging or ready the torso follows the zero-lead laser line and the gun fires only where its own leading solution agrees with that line | `AIM_SPLIT` |
| 1b | Laser release timing | `_laser_fire`: start only when roughly aligned; at full charge keep holding (still half speed) until aligned for release, with a 0.45 s cap so we never sit slow indefinitely; abort when retreating quietly | `LASER_HOLD` |
| 2 | Torso turn rate | the aim horizon adds decision latency (`LAT` 0.05 s) plus the time the torso needs at its real `turn_rate` / `pitch_rate` (gyro upgrades included); `_aligned` grants at most half a tick of slew and never more than half the tolerance; `_tracking_ok` refuses gun shots at a target whose angular speed exceeds 85% of the turn rate | `TURN_AWARE`, `LAT` |
| 3 | Enemy observations discarded by 2D LOS | an enemy listed by the server and inside our real sight region (45 m omni, or inside the 120 degree yaw wedge) counts as visible even if the 2D raster says blocked. **Risk:** a mech tracked for up to 5 s after it ducked behind a building also counts | `TRUST_OBS` |
| 4 | Incoming gun bullets | `_gun_dodge`: closest approach between the visible bullet and us (relative velocity, height band), dodge only if a sidestep can still clear it in time (`reach` from acceleration 58.5 m/s^2 and walk speed); weights by time to impact; hooked into `_incoming_dodge` | `DODGE_GUN` |
| 5 | Crate / drone fire that does nothing | `FARM_EFF`: 1.6 s of fire without 15 HP of damage -> reposition to a sampled stand point with 2D LOS; a second miss -> blacklist 60 s | `FARM_EFF` |
| 6 | Resource targets when direct steering fails | `FARM_PLAN`: after 2.5 s without progress the farm target becomes a point goal for the Dijkstra planner (`want = ("farm", id, x, y)`), following the planned polyline | `FARM_PLAN` |

### Priority 2 - threat-aware positioning (`THREAT_HIDE`, `ESC_W`, `SECT_W`, `EDGE_W`, `CHOKE_W`, `REHIDE_S`, `THREAT_MEM`)

Tracks of seen enemies (25 s memory) and sound fixes (bearing + near/mid/far band) feed `_threats()`. The 50 ms cycle only
builds a compact snapshot and compares a coarse signature (`_rehide_check`); the planner thread does the work in
`_select_hide`: the best 40 cells by the static score are re-ranked with `_threat_pen`: enemy reach time versus our travel time,
sight lines from the enemy, proximity, occupied bearing sectors (simultaneous attackers), whether an escape direction away from
all threats exists, hysteresis for the current spot. `EDGE_W` prefers the outer ring of the circle (zone-edge defence),
`CHOKE_W` prefers corridor cells when several hunters are known. Cost per plan: no measurable change (about 65-75 ms per plan
with or without threats, on the worker thread).

### Priority 3 - regeneration (`UPG`, `QUIET_RETREAT`, `QUIET_GRACE`)

* `UPG` profiles: `damage` (v2 order), `regen` (two nanite stacks first: x1.6 regen, -1.5 s delay each), `escape` (servos/jets/gyros first),
  `weapon` (by the second weapon slot). The n-th stack of an upgrade uses the n-th listed position.
* `QUIET_RETREAT`: while retreating, do not shoot (and abort a laser charge) once nothing has hit us for `QUIET_GRACE` = 2 s, unless the shot finishes the target (<=110 HP).
  While we are being hit, regeneration cannot run anyway, so we keep returning fire.
* While recovering (`recover_until`, HP < 85%) no *new* engagement is started (provoked, close and continuing fights stay).

### Priority 4 - selective offence (`ENGAGE = "ev"`, `EV_MIN`, `DEATH_COST`, `EV_RANGE`)

`_ev_engage` scores an *optional* fight (not provoked, not inside `CLOSE`, not already ours): p(win) from time-to-kill versus time-to-die,
our second weapon against theirs (laser > mortar > lightning > laser), lightning chain through clusters (+45% per extra target within 14 m,
max 2), every hunter around the target (visible, or tracked in the last 6 s within 110 m) added to the damage we expect to take, and a missing
escape direction multiplying the cost of losing. `EV = p*gain - (1-p)*cost`, `cost = DEATH_COST*(1 + 0.25*hunters)*(1.3 if no exit)`; engage when
`EV >= EV_MIN` and HP > 50%. `gain` is 1 (+0.3 for a laser against an isolated target). **`DEATH_COST` and the gain are tuning knobs, not measurements.**

### Priority 5 - lightweight experiments (`SCAN360`, `QUIET_FARM`, `THREAT_FARM`, `EDGE_W`, `CHOKE_W`)

`SCAN360`: while standing still and calm the torso sweeps the circle at 1.6 rad/s (v2 oscillates +-1.05 rad around the most open direction).
`QUIET_FARM`: no crate/drone gunfire while a hunter was seen/heard within 170 m in the last 10 s (a lightning second weapon is quiet enough).
`THREAT_FARM`: skip crates, cubes and pickups within 55 m of a sighting younger than 15 s. Rooftops and mortar zone-knockback were **not**
implemented (separate high-risk experiments per the brief).

## 3. Local evidence (my simulator; read with care)

All numbers: `tests/paired_eval.py`, deterministic simulator seeds, same seeds for every bot (paired differences; the paired standard
error is about 0.2 at 96 seeds). Points = 16 - place. The simulator is **my own model** with the same 2D line-of-sight as the bot, so it
cannot validate `TRUST_OBS` and its absolute scores (v2 = 4.24, official v2 = 3.31) are not official-score predictions.

| Config (96 seeds, `--know sense --diff 2`) | Points | paired delta vs v2 | kills | Reading |
| --- | --- | --- | --- | --- |
| v2 (frozen) | 4.24 +- 0.21 | - | 2.76 | baseline |
| v3 combat fixes only (priority 1 + quiet retreat with grace, `ENGAGE=off`) | 4.21 +- 0.22 | -0.03 +- 0.17 | 2.76 | neutral: the sim cannot see most of these corrections |
| + `ENGAGE=ev`, `EV_MIN=0.0` (**candidate default**) | **4.76 +- 0.25** | **+0.52 +- 0.21** | 3.48 | more kills at the same survival time |
| + `ENGAGE=ev`, `EV_MIN=0.35` / `0.7` | 4.41 / 4.41 | +0.17 / +0.17 | 3.09 | too strict: fewer fights |
| + `ENGAGE=ev`, `EV_MIN=-0.3` | 4.61 | +0.38 +- 0.19 | 3.52 | no better than 0.0 |
| + `ENGAGE=ev`, `DEATH_COST=1.0` | 4.58 | +0.34 +- 0.19 | 3.49 | no better than 2.0 |
| + `ENGAGE=ev`, `EV_RANGE=150` | 4.72 | +0.48 +- 0.22 | 3.68 | same within noise, lower survival time |
| candidate with `EV_RECOVER_GATE` on or off | 4.76 / 4.76 | identical | 3.48 | gate never changed a decision in these seeds |
| `THREAT_HIDE` (threat-aware hide cells) | 3.76 | -0.48 +- 0.22 | | harm comes from the selection itself (not re-planning) |
| `QUIET_RETREAT` without grace | -0.17 further | | | with a 2 s grace: neutral |
| `QUIET_FARM` + `THREAT_FARM` | 3.79 | -0.45 +- 0.18 | | cubes 3.7 -> 2.3 |
| `SCAN360` | -0.22 | | | |
| `EDGE_W` / `CHOKE_W` | -0.06 | | | |
| `UPG=regen` | about the same as `damage` | | | |

Robustness of the candidate default (`ENGAGE=ev`, `EV_MIN=0.0`; each against its own v2 baseline on the same seeds):

| Setting | seeds | v2 | v3 candidate (without the gate flag) | paired delta |
| --- | --- | --- | --- | --- |
| omniscient vision (`--know omni`), diff 2 | 1-64 | 4.19 | 4.31 | +0.12 +- 0.28 |
| `--know sense`, diff 3 (harder house bots) | 1-64 | 3.25 | 3.64 | +0.39 +- 0.24 |

All three settings point the same way (kills up, points up), but none of them is outside two paired standard errors on its own, and
the same 1-96 seeds were used to choose the threshold. An independent seed range (97-160) was queued; see section 5 for its result if present.

## 4. Rejected or unproven hypotheses

* **Threat-aware hiding (`THREAT_HIDE`) as a default:** -0.48 points in the sim. Rejected as a default, kept as a flag. Most likely the extra terms pull hides away from the static optimum (cover, zone distance) more than the threat knowledge pays back in a model whose hunters are coarse.
* **Quiet farming and threat-aware collection (`QUIET_FARM`, `THREAT_FARM`):** -0.45 points (cubes collected 3.7 -> 2.3). The quieter policy costs more cubes than it saves in fights, in the sim. Off.
* **360 degree calm scan (`SCAN360`):** -0.22. Off. The v2 oscillation already covers the open direction.
* **Zone-edge preference and chokepoints (`EDGE_W`, `CHOKE_W`):** -0.06 each, within noise. Off.
* **Regeneration upgrade profile (`UPG=regen`):** no difference from the damage-first order in the sim. Default stays `damage`.
* **Quiet retreat without a grace period:** -0.17. With the 2 s grace it is neutral; kept on because it lets regeneration actually start.
* **Rooftops and mortar knockback:** not implemented (separate high-risk experiments in the brief).
* **Not rejected, not proven:** every priority-1 correction (weapon-specific aim, turn rate, trusting server observations, bullet dodging, farm efficiency / planner fallback). They are on by default because they follow from the rules and are neutral in the simulator, which cannot represent most of them. Their official effect is unknown.

## 5. Validation

* Unit tests: `tests/test_v2.py` (32) and `tests/test_v3.py` (50): 82 tests pass.
* Equivalence: `tests/check_equivalence.py` (v3 with every v3 flag off versus the frozen v2): per-seed identical on seeds 1-12 earlier and 1-6 after the final edit.
* Official protocol checker `check/check.py` on the packaged tree, three modes, all PASS with 300/300 answered in time:
  plain (p99 6.2 ms, max 9.5 ms), pinned to one CPU with `taskset -c 0` (p99 3.8 ms, max 4.8 ms), and launched as uid 65534 on one CPU (p99 5.8 ms, max 6.4 ms).
  The checker replays a recorded stream; it proves protocol and timing, not play strength.
* **Not run:** the official Docker conformance run (there is no Docker daemon in this environment). The `Dockerfile` is unchanged in structure from v2 (standard library only, `COPY mechfight.py nav.py ghost_bot.py`).
* Planning cost: about 65-75 ms per plan on the worker thread with or without threat terms; the 50 ms answer path only reads the cached plan.

## 6. What is still unknown

* Whether the real server applies the torso slew before or after the shot leaves in the same tick (assumed: at most half a tick of credit).
* Whether `TRUST_OBS` helps (the simulator uses the same 2D LOS for visibility, so it cannot show it).
* The real hunters' policy; the simulator's hunters are a model of "coordinated, share vision, hunt the weakest first".
* Official-score noise: standard error is about 0.4 per 48-match run (estimate), so one run cannot separate changes smaller than about 0.8.
