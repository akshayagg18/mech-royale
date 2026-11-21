# Ghost-v1 vs Ghost-v2 in the squad-hunt simulator (`tests/arena_sim.py`)

A model I wrote, NOT the real game: 16 mechs on the real arena, 15 scripted hunters that share vision, never fight each
other and go for the weakest first. `--know omni` = hunters know our position, `--know sense` = they find us through
vision and sound. `--diff` scales hunter skill/aggression. At diff 2-3 the simulator's v1 scores 2.7-3.7, which brackets the
real 2.48, so the simulator is still somewhat easier than the real house bots.

Command: `python3 tests/arena_sim.py --bot <ghost_bot_v1|ghost_bot> --seeds 1-32 --know <omni|sense> --diff <2|3> --procs 2`
Same 32 seeds for both bots, v2 at its shipped defaults, 0 errors in all eight runs. Standard error of one cell is about
0.5 points (per-match standard deviation about 2.9).

| Hunters | diff | v1 points (kills, survival) | v2 points (kills, survival) | v2 - v1 |
| --- | --- | --- | --- | --- |
| omni | 2 | 3.34 (2.34 kills, 99 s) | 3.78 (2.56, 94 s) | +0.44 |
| sense | 2 | 3.66 (1.75, 118 s) | 4.16 (2.88, 98 s) | +0.50 |
| omni | 3 | 2.72 (1.75, 87 s) | 3.25 (2.28, 92 s) | +0.53 |
| sense | 3 | 2.94 (1.53, 96 s) | 3.09 (2.44, 80 s) | +0.15 |
| **mean of the four** | | **3.17** | **3.57** | **+0.40** |

Reading it honestly:
* All four differences are positive and mean kills rise in all four (1.75 -> 2.88 in the largest), but each is inside the
  noise of its cell. This is "no sign of harm, a consistent small gain", not proof.
* v2 trades survival time for kills (it lives 5-20 s shorter in three of four cells but kills 0.2-1.1 more). Points are
  `16 - place`, so what matters is how many hunters die before we do; the simulator's hunters die only to us or to the zone.
* In the real game the house bots, vision and drops behave differently; the sign of the effect is the thing to test officially.

## One-factor ablations run while building v2 (smaller seed counts, same noise)
* `FARM` on is better than off (direction consistent, inside the noise).
* `OPP` (opportunistic engagement of favourable targets) on: equal or worse than off, so it ships off.
* `RECOVER` on/off: neutral.
