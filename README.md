# ghost-v1: a survival-first Mech Royale bot

A `mechfight/1` bot for the Builders Buildathon 2026 Mech Royale track. Python 3, **standard library only**,
built on the official `mechfight.py` wrapper (copied verbatim).

Scoring is placement by death order, so this bot optimises *staying alive* and picks fights only when it has to:

* **Zone first.** Stays inside the *next* safe circle. Because a shrinking circle always contains the next one, being
  inside it means zero zone damage. Planned paths are zone-aware (soft cost outside the circle, a hard "stay inside"
  re-plan when the cheapest route would leave it), and hiding spots are taken from the largest connected region of the
  next circle so later circles stay reachable (cliffs, buildings and rivers can split a circle).
* **Real navigation.** Rasterises `world.json` prisms and `terrain.i16` slopes into a 4 m grid (map loaded in a worker
  thread, ~0.2 s), Dijkstra with a clearance cost, string-pulled paths, pure-pursuit following, stuck detection that
  learns blocked cells.
* **Hide, don't hunt.** Picks low-exposure cells (line-of-sight rays) inside the next circle, idles there (no noise,
  armour facing the last threat, regen after 5 s without damage), and moves again when the zone says so. When calm and
  early enough it walks below the 5 m/s footstep threshold (silent) and only sprints when the schedule demands it.
* **Selective combat.** Fires only when provoked, when something is within 50 m, or to finish a weak mech. Strafes,
  keeps its front arc toward the target (rear armour is x1.5), dodges mortar shells and laser charges, and breaks off
  to cover below 35% HP.
* **Opportunistic loot.** Supply drops when uncontested, nearby laser/lightning pickups and cubes inside the safe zone,
  upgrade offers via a fixed priority list.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | python:3.13-slim, copies the three modules, entrypoint `ghost_bot.py` |
| `ghost_bot.py` | the bot (decisions, combat, planning requests) |
| `nav.py` | map rasterisation, Dijkstra, line of sight, exposure, zone penalty, connected components |
| `mechfight.py` | official wrapper, unmodified |
| `tests/` | kinematic simulator (`sim.py`) and a synthetic-map generator; not the real game |
| `docs/research.md` | verified facts vs hypotheses from the rules and protocol |

## Checking it

```sh
# protocol conformance (needs the official kit's check/ directory)
python3 check/check.py -- python3 -u ghost_bot.py
docker build -t ghost .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only \
    --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" ghost

# zone-survival simulation on the real arena (arena/v2/{world.json,terrain.i16} from the kit)
python3 tests/sim.py --map path/to/arena --seed 1 --seconds 420
python3 tests/sim.py --map path/to/arena --seed 1 --seconds 420 --chaos --enemies 5   # exercises combat paths
```

The simulator models walking, collisions, the zone and a few scripted enemies; it exists to catch navigation and
zone-handling bugs, not to predict match results.

## License

MIT, see `LICENSE`.
