# Experiments (kinematic simulator, NOT the real game)

`tests/sim.py` models walking/collision on the real map raster, the zone schedule, regen, mortar shells, drops and a few
scripted hunters. The planner's worker thread is modelled deterministically (result lands 3 decisions = 0.3 s after the
request). It exists to catch navigation / zone-handling bugs. It does not predict match scores.

## Zone survival, no enemies, 420 s (command: `python3 tests/sim.py --map <arena> --seed N --seconds 420`)
All zone circles collapse to a point at ~415 s, so every mech alive then dies of zone damage within seconds; the goal is
"no damage until the final collapse".

| Map | Seeds | Died at (s) | Notes |
| --- | --- | --- | --- |
| Real arena (final code) | 1..6 | 405.6, 417.8, 418.6, 416.8, 418.4, 418.7 | min margin vs. circle -2.3 .. -9.1 m, only in the last circle |
| Synthetic, final code | 2, 4 | 419.8, 412.2 | seed 4 = final point on far side of a cliff, only crossing 140 m outside the zone |

Noise (seconds moving faster than 5 m/s, real arena seeds 1-4): stealth off 82.4 / 43.2 / 103.0 / 35.0 -> stealth on
61.8 / 15.1 / 61.8 / 16.9, with survival unchanged (405.2->407.0, 418.1->417.8, 418.3->418.6, 416.7->417.0).

## Bugs found and fixed with these runs
1. Stale plans: stuck/deviation handlers re-requested every ~2.5 s while a request was in flight, so finished plans were
   discarded (version mismatch) and the bot never left a pocket. Fix: `_replan` is rate-limited, results are also accepted
   when their `want` still matches.
2. Planner ignored the zone boundary, so routes around a wall could leave the circle -> "zone" mode steered straight into
   the wall. Fix: soft zone penalty in Dijkstra, plan-following in "zone-go" mode, hard-zone second pass.
3. Hide spot chosen on the wrong side of a barrier that splits the next circle. Fix: biggest connected component preference.
4. `_loot` crashed on pickups without a z coordinate (sim data); now `zof()` tolerates both.

## Stress test (hostile scripted hunters, `--chaos --enemies N`)
No exceptions in any run (combat, dodge, loot, drop, upgrade pick paths exercised). The scripted hunters are far more
accurate than a real opponent, so survival there (34-326 s) is a stress result, not a benchmark.

## Official checks on the final code
`check/check.py`: plain, `taskset -c 0` (1 CPU) and as uid 65534 with cwd /tmp: 300/300 answered in time, 0 late, 0 wrong
tick, 0 over the debug caps, PASS in all three. Replay of the 300 recorded real observations: 0 exceptions.
Peak RSS (sim + bot together): 29 MB. Not run: the Docker image build (no Docker daemon in this workspace).

## Ghost-v2 vs Ghost-v1 (squad-hunt simulator)
See `v1_vs_v2.md`: v2 +0.40 points on average over four conditions (32 seeds each), each inside the noise; kills up in all four.
Official checks for v2: `check/check.py` plain / 1 CPU / uid 65534 all PASS, 300/300 in time, p99 2.7-7.5 ms, max 8.1 ms.
`tests/test_v2.py`: 32 tests, all pass.
