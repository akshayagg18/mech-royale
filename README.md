# ghost-v3: a Mech Royale bot that builds up, aims by weapon, and picks its fights

A `mechfight/1` bot for the Builders Buildathon 2026 Mech Royale track. Python 3, **standard library only**, built on
the official `mechfight.py` wrapper (copied verbatim). `ghost-v2` (practice score 3.31, commit `f0b1fb3`) is kept unchanged in
`baseline/ghost_bot_v2.py`, `ghost-v1` (2.48) in `baseline/ghost_bot_v1.py`; `PRESET = "v2"` in `ghost_bot.py` turns every v3
change off. There is **no official v3 score yet**; see `experiments/LEDGER.md` for the exact code behind each official score.

Scoring is placement by death order against coordinated house bots that hunt the weakest team first, so v3 keeps v2's zone
discipline, navigation and build-up and corrects the combat layer:

* **Weapon-specific aim.** Gun shots intercept the target; laser and lightning are hitscan (no flight-time lead). The torso
  follows the real `turn_rate`: alignment credit is capped, targets we cannot track are not shot at, a charged laser is held
  until aligned (0.45 s cap) instead of releasing a misaimed beam.
* **Observations are trusted.** An enemy the server lists inside our sight region is visible even if the 2D map disagrees.
* **Bullet dodging.** Visible incoming gun bullets are sidestepped when a lateral move can still clear them.
* **Farming that checks itself.** Sustained fire on a crate or drone that loses no HP triggers a reposition, then a blacklist;
  stalled approaches are handed to the Dijkstra planner.
* **Quiet retreat.** When retreating and nothing is hitting us, stop shooting so the 5 s regeneration clock can run; no new
  fights while recovering.
* **Selective offence (`ENGAGE = "ev"`).** Optional fights are scored by expected placement points: kill-time vs death-time,
  weapon triangle, lightning clusters, hunters standing around the target, escape routes.
* **Experiments, off or on by flag:** threat-aware hiding, 360 degree scanning, quiet / threat-aware farming, zone-edge and
  chokepoint positioning, upgrade profiles (`UPG`).

Every behaviour is a flag or number in the `CFG` dict at the top of `ghost_bot.py`, so official runs can change one thing at a
time. `experiments/V3_IMPLEMENTATION.md` lists each flag, its default and the local evidence.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | python:3.13-slim, copies the three modules, entrypoint `ghost_bot.py` |
| `ghost_bot.py` | ghost-v3 (decisions, farming, combat, planning requests) |
| `nav.py` | map rasterisation, Dijkstra, line of sight, exposure, zone penalty, connected components |
| `mechfight.py` | official wrapper, unmodified |
| `baseline/ghost_bot_v1.py`, `baseline/ghost_bot_v2.py` | the frozen v1 (commit `e3fbf79`) and v2 (commit `f0b1fb3`, official 3.31) bots |
| `tests/test_v2.py`, `tests/test_v3.py` | 32 + 50 decision tests on the real map (farming, fights, retreat, zone push, drops, aim, laser, bullets, threat-aware hiding, EV engagement, protocol edge cases, latency) |
| `tests/paired_eval.py`, `tests/check_equivalence.py` | paired multi-config simulator runs; v3 with `PRESET = "v2"` must equal the frozen v2 |
| `tests/arena_sim.py` | 16-mech squad-hunt simulator on the real arena; `--bot ghost_bot_v1` / `ghost_bot`, `--cfg KEY=VAL` |
| `tests/sim.py` | kinematic zone-survival simulator |
| `experiments/` | `V3_IMPLEMENTATION.md`, `ROOT_CAUSE_ANALYSIS.md`, `LEDGER.md`, simulator results |
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
python3 tests/test_v3.py --map path/to/mechfight-kit/arena

# paired v2 vs v3 in the squad-hunt simulator (a model I wrote, NOT the real game: noise is about 0.2-0.5 points)
python3 tests/paired_eval.py --seeds 1-48 --know sense --diff 2 base=ghost_bot_v2 v3=ghost_bot v3_ev=ghost_bot:ENGAGE=ev
python3 tests/check_equivalence.py
```

The simulators exist to catch bugs and rule out ideas that clearly hurt. They cannot predict the official score.

## License

MIT, see `LICENSE`.
