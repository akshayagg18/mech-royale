# Root-cause analysis — why Ghost-v1 scored 2.48 (rank 77/103)

Written Sat 2026-11-21 (SGT). Inputs: the code of `src/ghost_bot_v1.py` (frozen, identical to commit `e3fbf79`), the
official first result (2.48, rank 77 of 103 at submission; leader 5.25; baseline 1.15; average placement about 13.52 of 16),
the rules in `research.md`, the official checker, and the arena simulator in `tests/arena_sim.py`.

## How to read this

* **Confirmed defect** = visible in the code / reproducible by a test, independent of how the real game plays.
* **Hypothesis** = a claim about how the real house bots or the real game behave. We have exactly one official number and
  no per-match telemetry, so *none of the gameplay claims below is verified*. The simulator is a model I wrote; it is useful
  for catching bugs and for ruling out ideas that clearly hurt, not for predicting the score (its run-to-run standard error is
  about 0.5 points, and the real score's is about 0.4).
* Each item: suspected problem, exact code location, evidence, confidence, fix, test, expected benefit and implementation risk.
* "v1" line numbers refer to `src/ghost_bot_v1.py`; "v2" to `src/ghost_bot.py`.

## What the 2.48 means (arithmetic, not a finding)

Points are `16 - place`, so 2.48 is an average finishing place of 13.52. House bots only die to us, to drones or to the zone,
so a team's points are roughly "how many mechs die before we do". Ghost-v1 therefore outlasts about 2.5 of 15 opponents on
average. The 1.15 baseline outlasts about 1. The leader outlasts about 5. Reaching the top 32 needs roughly 4.0-4.1 (read from
the live board), i.e. about one and a half more mechs dying before us per match. Two ways to get there: die later (survive
better) or make more of them die (kill them, or let drones and the zone do it while we are still alive).

---

## Ranked findings

Rank = confidence x expected benefit, highest first.

### RC-1 — No proactive crate destruction (Confirmed defect; benefit is a gameplay hypothesis)
* **Problem:** v1 never reads `obs["crates"]`. 36 crates x 300 HP each drop a cube (+4% max HP and +4% damage per cube) and
  half of them a weapon. A mech that never breaks crates never gets stronger, and never finds the lightning/laser that the
  rest of the field uses.
* **Location:** v1 `act()` (l.424 onwards) and `_loot` (l.766): there is no reference to crates anywhere. Searching the file
  for `crates` returns nothing.
* **Evidence:** code search (above). Rules: crate 300 HP, 1 cube, 50% weapon. The gun does 45 damage per shot at 0.24 s, i.e.
  7 shots, about 1.7 s per crate.
* **Confidence:** that the defect exists: certain. That fixing it moves the score: likely but not measured (the simulator
  agrees in direction: switching `FARM` off lowers the score; the effect is inside the noise on small seed counts).
* **Fix (v2):** `_farm()` (candidates: crates, drones, cubes, laser/lightning pickups). Crate value 3.2, +1.2 when the second
  weapon slot is empty; only with line of sight, inside the next zone circle with a margin, within +-7 m height, closer than
  70 m; approach to 30 m then shoot with the gun; 3.5 s (pickups, cubes) or 6 s (drones, crates) without progress blacklists
  the target for 45 s.
* **Test:** `FarmTests` in `tests/test_v2.py` (shoots in range and holds position, approaches when far, ignores crates outside
  the next circle / below the HP floor / with an enemy in view / late in the match).
* **Benefit / risk:** benefit: several cubes and usually one weapon before the first hunters arrive. Risk: farming exposes us
  (noise from shooting, standing in the open). Mitigated by the gating above, a 55% HP floor and `FARM_UNTIL = 200 s` for
  crates. Remaining risk: unmeasured; shots are loud (gun 320 m).

### RC-2 — Drones are only fought after they hit us (Confirmed defect; benefit is a hypothesis)
* **Problem:** 12 drones x 350 HP, 2 cubes each, 25-damage bullets at 1 per second (weak). v1 fights one only for 4 s after a
  drone hit us.
* **Location:** v1 l.549 (`if fight is None and t - self.last_npc_hit < 4.0`).
* **Evidence:** code; rules above. Drones leash at 30 m and chase up to 60 m, so engaging at 30 m with a gun is nearly free.
* **Confidence:** defect certain; benefit likely (2 cubes per 350 HP is the cheapest cube source on the map).
* **Fix (v2):** drones are `_farm` candidates (value 4.4, within 75 m, nearest first), plus the old self-defence rule.
* **Test:** `test_npc_nearest_preferred_and_shot`.
* **Benefit / risk:** as RC-1; drone fire is 25 dmg/s so the HP floor is safe.

### RC-3 — Loot radii were too small and weapon pickups were not prioritised (Confirmed defect)
* **Problem:** weapon pickups only within 38 m, cubes within 26 m; supply drops only if within 110 m. 40 floor weapons exist;
  lightning is the best weapon by a wide margin (see RC-4/hypotheses). A supply drop is +3 cubes, full HP and energy.
* **Location:** v1 `_loot` (l.766-800: `dh > 38`, `dh > 26`), `_drop_target` l.757 (`dd > 110`).
* **Evidence:** code. In v2 the weapon radius is 90 m (value 7 for lightning/laser, 5.5 second), cubes 60 m early / 30 m later,
  drops 260 m (`CFG["DROP_REACH"]`) with the existing "arrive no earlier than landing" rule.
* **Confidence:** defect certain; benefit likely for drops and weapons.
* **Test:** `test_lightning_pickup_beats_laser_and_cube`, `test_pickup_ignored_when_slot_full_and_mortar_ignored`,
  `test_take_action_only_in_reach_and_only_good_weapons`, `DropAndSoundTests`.
* **Risk:** longer walks are louder; walking is capped at the 5 m/s no-noise speed only when time allows (unchanged).

### RC-4 — Combat is initiated only reactively (Confirmed code behaviour; "too late" is a hypothesis)
* **Problem:** v1 fights only when it was hit in the last 4 s, an enemy is under 50 m, or a target is at <= 30% HP.
* **Location:** v1 l.538-547.
* **Evidence:** code. The squad hunts us weakest-first, so most fights start on their terms (first contact is the first
  shot at us). Counter-evidence from the simulator: v2 with opportunistic engagement on (`OPP`) was not better than with it off
  (equal or worse), so it ships **off**. That is a simulator result against hunters I wrote, not a statement about the real bots.
* **Confidence:** that v1 is purely reactive: certain. That earlier initiation helps: low (an experiment, see "next experiments").
* **Fix (v2):** `_choose_fight()` makes the decision from a small utility model (below). The close threshold is 55 m (was 50),
  provoked targets win ties, a committed target keeps a hysteresis bonus, the preferred range depends on the loadout
  (`_pref_range`: lightning 24 m, laser 80 m, gun 48 m, 62 m or more against lightning when we have none).
  `CFG["OPP"]` and `OPP_RANGE`, `OPP_MAXN` expose opportunistic engagement for an official A/B.
* **Test:** `FightTests` (provoked fought at range, unprovoked distant not attacked, `OPP` switch, preferred ranges).
* **Benefit / risk:** unknown; the risk of `OPP=True` is starting fights we would not have been in.

### RC-5 — Flat 35% retreat and no recovery state (Confirmed code behaviour; threshold choice is a hypothesis)
* **Problem:** `flee = hp < 0.35 * max_hp` regardless of how many enemies are close, whether we are winning, or whether the
  enemy can still reach us; fleeing mechs never get a deliberate recovery phase (regen is 45 HP/s after 5 s without damage).
* **Location:** v1 `_combat` l.728 (`flee = ...`), l.739-740 (`goal * 0.35`).
* **Evidence:** code; rules (regen 45 HP/s after 5 s; hunters work as a squad, so one enemy in view is rarely the only one).
* **Confidence:** defect (rigidity) certain; the exact thresholds are guesses.
* **Fix (v2):** retreat threshold `RETREAT + 0.07 * (n_close - 1)` = 28% alone, 35% with two, 42% with three; no retreat when
  we kill it faster than it kills us (`tk < 0.5 * td`); below the threshold: if the nearest enemy is farther than
  `RECOVER_RANGE` (90 m) the state is **recover** (no firing, break contact, hold for 7 s or until HP > 85%); inside 90 m it is
  **flee** (keep shooting while backing to cover). Hysteresis: `recover_until`, `fight_id` bonus.
* **Test:** `test_retreat_threshold_scales_with_crowd`, `test_far_enemy_below_threshold_means_recover_not_fire`,
  `test_recover_does_not_flip_back_before_hp_returns`, `test_winning_fight_is_not_abandoned`.
* **Benefit / risk:** the simulator shows `RECOVER` as neutral. Kept on because it is cheap; one switch in `CFG`.

### RC-6 — Combat overrides zone-aware navigation (Confirmed defect)
* **Problem:** `_combat` only wall-steers (`_steer`) and drifts toward the goal with weight 0.35. It has no knowledge of the
  zone, so a strafing duel at the rim walks us out of the next circle and into 20-260 dps damage; the zone damage also blocks
  the 45 HP/s regeneration.
* **Location:** v1 `_combat` l.686-745 (no reference to `circle` except in `_cover_dir` when fleeing).
* **Evidence:** code only. I did not measure how often this happens in the simulator or in real matches.
* **Confidence:** certain that the code has no zone term in combat; how often it costs HP is unmeasured.
* **Fix (v2):** `_duel` adds a push toward `self._zplan` when the margin to the next circle falls below `ZONE_BUF` = 14 m,
  proportional to the shortfall; strafe flips when the path is blocked.
* **Test:** `test_duel_movement_pushes_back_inside_the_zone`.
* **Benefit / risk:** removes a free source of damage. Risk: slightly worse strafing at the rim.

### RC-7 — Sounds are ignored (Confirmed gap; benefit small)
* **Problem:** shots are audible far beyond sight (gun 320 m, laser 380 m); the observation carries a bearing (sigma 7 deg) and
  a distance band. v1 never reads `obs["sounds"]`.
* **Location:** v1 `act()` (no `sounds` reference).
* **Fix (v2):** `SOUND_AIM`: when standing still and a gun/laser was heard within 3 s, face it (the 120 degree vision wedge
  reaches 200 m, the omni view only 45 m; facing also keeps the armoured front toward the shooter).
* **Test:** `test_sound_pre_aim_when_standing_still`.
* **Benefit / risk:** small, near zero risk.

### RC-8 — Latency / compute overhead (Refuted as a cause)
* **Question:** did a slow `act()` cost us actions? No.
* **Evidence:** official checker, three modes (plain, 1 CPU via `taskset`, uid 65534): 300/300 answered in time, 0 late.
  `act()` p50 0.2-1.6 ms, p99 2.7-7.5 ms, max 6.4-8.1 ms (budget 50 ms). Peak memory about 29 MB (limit 512 MB). `ready` in
  37-68 ms (limit 20 s). Planner runs on a worker thread; `act()` never blocks on it. `test_decision_latency_budget` pins
  p99 < 10 ms over 600 decisions with crates and enemies present.
* **Caveat:** the simulator's SyncBot shows `act_ms_p99` of about 80 ms; that is an artefact of planning inline in the
  synchronous model, not the shipped threaded design. The measured threaded figure is p99 1.27 ms, max 2.84 ms on one pinned CPU.
* **Confidence:** high for this machine; the scoring container may be slower but there is a 7x margin at p99.

### RC-9 — Hide-first may simply be found (Hypothesis, unverified, highest strategic uncertainty)
* **Claim:** the house squad shares vision and hunts weakest-first, so hiding gives the squad time and information, while
  every weapon/cube the bot did not collect makes it the weakest target. Part of what we fixed (RC-1..3) is "stop being weak".
* **Evidence:** only the 2.48 itself (about 2.5 mechs dying before us), the page text ("one early, more as you thin them out,
  all late"), and a leader at 5.25 whose approach we do not know.
* **Confidence:** low. Needs per-match telemetry (time and cause of death) that the portal does not currently show us.
* **How to test:** official submissions that change one thing (see `LEDGER.md`); the portal feedback after a run, if it
  includes per-match results, is the first thing to read.

---

## Hypotheses I considered and did not implement

| Idea | Why not (yet) | What would make me try it |
| --- | --- | --- |
| Rooftops / high ground | Needs a height-aware exposure model; `nav.py` rasterises ground height only; no evidence the house bots shoot worse uphill | Telemetry showing deaths to long-range fire in the open |
| Controlled zone exposure (take zone dps to stay out of fights) | The last circle is lethal (260 dps); only helps in phases 1-2 (20-35 dps), and costs the 45 HP/s regen | A run where deaths cluster in a fight at the zone rim |
| Power scaling / upgrade order | Already reordered (gun damage, plating, armor first; the old order opened with nanites); gain is small next to cubes | Per-upgrade results from the portal |
| Aggro management (bait one hunter, finish the weak one) | Needs reliable enemy identity and HP tracking over time; vision and tracking rules make this fragile | A stable gun-plus-lightning loadout and a positive `OPP` result |
| Position scoring beyond exposure (cover quality, sight lines) | `nav.exposure` already scores cells; improving it cannot be validated without real matches | Same as RC-9 |

## The utility / risk model that replaced "hide first"

A rule-based utility model, not a learned one. States and the code that implements them:

| State | When | Code |
| --- | --- | --- |
| Early resource build-up | calm (no visible enemy within `CALM_R` = 100 m, no damage for 3 s), HP above 55%, inside the next circle; crates and drones only while `t < FARM_UNTIL` = 200 s; cubes (30 m after that) and weapon pickups until 380 s | `_farm` |
| Controlled engagement | provoked, close (< 55 m), weak target (<= 30% HP), or committed | `_choose_fight` -> `duel`, `_duel` |
| Escape / recovery | HP below `RETREAT + 0.07 (n - 1)` and not winning | `recover` (far) or `flee` (near) |
| Positional survival | otherwise: go to and hold a low-exposure cell of the next circle | unchanged v1 planner, `hide` / `go` |
| Late-game survival | last circles: no farming after 380 s, zone mode, boost back inside | unchanged v1 zone logic |

Hysteresis: the current farm target gets x1.5 and a stalled one is blacklisted for 45 s after 3.5-6 s without progress; `fight_id` keeps a
-15 m bonus; `recover_until` holds the recovery state for 7 s; the stuck detector includes the "farm" mode.

## Confirmed vs hypothesis, in one table

| Item | Defect confirmed | Gameplay effect confirmed |
| --- | --- | --- |
| RC-1 crates | yes | no |
| RC-2 drones | yes | no |
| RC-3 loot radii / drops | yes | no |
| RC-4 reactive combat | yes (behaviour) | no (early engagement not helpful in the sim) |
| RC-5 flat retreat | yes (rigidity) | no |
| RC-6 zone vs combat | yes | no (frequency unmeasured) |
| RC-7 sounds | yes (gap) | no |
| RC-8 latency | refuted | n/a |
| RC-9 hide-first found | n/a | no, unverified |

The only real test is an official run. `LEDGER.md` lists them in the order I would run them.
