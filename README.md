# ghost-v2: a Mech Royale bot that builds up, then fights on its own terms

A `mechfight/1` bot for the Builders Buildathon 2026 Mech Royale track. Python 3, **standard library only**, built on
the official `mechfight.py` wrapper (copied verbatim). `ghost-v1` (survival-first, scored 2.48 in practice) is kept
unchanged in `baseline/ghost_bot_v1.py` for A/B comparison.

Scoring is placement by death order against coordinated house bots that hunt the weakest team first, so v2 keeps v1's
zone discipline and navigation but stops being the weakest target:

* **Zone first (unchanged).** Stays inside the *next* safe circle (zero zone damage), zone-aware Dijkstra on a 4 m grid
  rasterised from `world.json` / `terrain.i16`, low-exposure hiding cells from the largest connected region.
* **Build up while calm.** Breaks crates, shoots drones, collects cubes, and walks to laser/lightning pickups (90 m) and
  supply drops (260 m) when no enemy is within 100 m and HP is above 55%. Crates and drones only in the first 200 s.
* **Controlled engagement.** Fights when provoked, when an enemy is within 55 m, or to finish a weak target; picks its
  range from the loadout (lightning 24 m, laser 80 m, gun 48 m), leads shots, holds and releases laser charges, and
  never starts a charge with a second enemy close.
* **Retreat and recovery.** Retreat threshold 28% HP, plus 7% per additional close enemy, unless it is winning on kill time;
  breaks contact (no firing) when the enemy is out of reach, keeps shooting while backing away when it is not.
* **Zone-aware duels.** Duel movement is pushed back toward the next circle near its rim.
* **Sounds.** Faces gunfire when standing still.

Every behaviour above is a flag or number in the `CFG` dict at the top of `ghost_bot.py`, so official runs can change one
thing at a time (see `experiments/LEDGER.md`).

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | python:3.13-slim, copies the three modules, entrypoint `ghost_bot.py` |
| `ghost_bot.py` | ghost-v2 (decisions, farming, combat, planning requests) |
| `nav.py` | map rasterisation, Dijkstra, line of sight, exposure, zone penalty, connected components |
| `mechfight.py` | official wrapper, unmodified |
| `baseline/ghost_bot_v1.py` | the frozen v1 bot (commit `e3fbf79`) |
| `tests/test_v2.py` | 32 decision tests on the real map (farming, fights, retreat, zone push, drops, protocol edge cases, latency) |
| `tests/arena_sim.py` | 16-mech squad-hunt simulator on the real arena; `--bot ghost_bot_v1` / `ghost_bot`, `--cfg KEY=VAL` |
| `tests/sim.py` | kinematic zone-survival simulator |
| `experiments/` | `ROOT_CAUSE_ANALYSIS.md`, `LEDGER.md`, simulator results |
| `docs/research.md` | verified facts vs hypotheses from the rules and protocol |

## Checking it

```sh
# protocol conformance (needs the official kit's check/ directory)
python3 check/check.py --map-dir arena -- python3 -u ghost_bot.py
docker build -t ghost .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only \
    --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" ghost

# decision tests (real map from the kit)
python3 tests/test_v2.py --map path/to/mechfight-kit/arena

# v1 vs v2 in the squad-hunt simulator (a model I wrote, NOT the real game: noise is about 0.5 points)
python3 tests/arena_sim.py --map path/to/arena --bot ghost_bot_v1 --seeds 1-24 --know omni --diff 2
python3 tests/arena_sim.py --map path/to/arena --bot ghost_bot    --seeds 1-24 --know omni --diff 2
```

The simulators exist to catch bugs and rule out ideas that clearly hurt. They cannot predict the official score.

## License

MIT, see `LICENSE`.
