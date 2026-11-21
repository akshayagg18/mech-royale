# Mech Royale — research notes (Agent 1)

Written 2026-11-21 (UTC) before implementation. **[V]** = verified against an official source
read during this session. **[H]** = hypothesis / inference, untested.

## 1. Hackathon facts

Sources: <https://www.joinbuilders.org/buildathon>, `/buildathon/tracks/builders`,
`/buildathon/leaderboard`, the kit's `README.md` + `protocol.md`.

- [V] Builders Buildathon, Mech Royale track: $5,000 to the top **final** score. 57 teams on the
  track page. Submissions close **Sun 2026-11-22 11:00 ET** (the page showed "Scoring closes in
  ~39 h" at 2026-11-21 ~08:55 SGT). Closing ceremony 12:00 ET; leaderboards freeze there.
- [V] Entry rules: one entry per track per team at a time (queued entries count). Every submission
  counts toward the *hourly* limit, even rejected ones ("hour limit reached" shows on the
  leaderboard for some teams). Rejected entries show a reason; best score is kept. Final entry =
  the submission you select, else best-scoring one. Organizers verify top results.
- [V] Everything built must be MIT-licensed (so the repo needs a `LICENSE`). Any language, library,
  AI tools allowed; name third-party dependencies in the README.
- [V] Scorer version `builders-3`. Score = mean over matches of `16 - place`; ties on score are
  broken by earlier submission. Baseline row: the kit's Python example bot = **1.15**.
- [V] Practice = 48 matches vs built-in bots. "Top teams qualify for additional evaluation against
  other competitors on new seeds" (task brief) — the official page only says each track page
  explains its final scoring; the Builders page itself does not detail the final.
- [V] Build: public GitHub repo, `Dockerfile` at repo root, <= 20 MB at the submitted commit,
  linux/amd64, network ON during build, <= 10 min. Pin dependency versions (finalists are rebuilt).
- [V] Runtime: no network, 1 CPU, 512 MB, read-only FS + 64 MB `/tmp`, uid 65534, <= 128
  procs/threads, map mounted read-only at `/arena`.
- [V] Leaderboard (page text, unsorted, partly ambiguous): top visible scores 5.17 (team "AT"/01AT),
  4.96, 4.73, 4.69, 4.58, 4.33, 4.13; Baseline 1.15. Only top-5 rows per track are rendered.
  A score of 5.17 means mean place ~10.8 of 16, i.e. **even the leaders finish below the median of a
  uniform field** — the built-in bots are clearly competent. Random play would score 7.5 against
  equals, so the field is strong or the practice opponents are stronger than average.
- [V] There is no local simulator in the kit; the only game feedback is the official scorer
  (rate limited per hour) plus the browser viewer <https://play.joinbuilders.org>.
- [V] The track page's "Sign in" button was visible when read: submission needs a signed-in team
  account (human).

## 2. Protocol (mechfight/1) — verified from `protocol.md`

Full spec is in the kit; the parts that drive strategy:

- Lockstep, 10 decisions per match-second (20 ticks/s, action lasts 2 ticks), **50 ms** per
  decision wall-clock, `ready` within 20 s. Late answers are dropped; continuous fields
  (`move`, `aim`, `boost`) carry over, one-shot fields do not; after `coast_s`=1 s of misses the mech
  idles. Crash = idle for the rest of the match.
- Placement = order of death; same-tick deaths share a place; at 8:00 survivors ranked by HP.
- Map: 1070x724 m OSU campus, 259 prisms (223 buildings, 27 stadium parts, 9 bridges), 1560 trees,
  terrain heightmap 1 m grid (range -9.5..49.2 m). Same every match; spawns/crates/NPCs/loot/zone
  centres/supply drops are random.
- Zone: start r=560 @ (41.3,-72.8); phases (hold+shrink s, radius, dps): 60+50→380 @20, 45+40→240 @35,
  35+35→140 @60, 30+30→70 @100, 25+25→25 @160, 15+25→0 @260. Last circle closes at ~415 s (6:55).
  `obs.zone.next_center/next_radius` is **always visible**, including at t=0.
  - [V, derived] The interpolated circle at any time during a shrink contains the entire next circle,
    so a mech that stays inside the *next* circle takes **zero** zone damage, ever.
- Health: 1400 HP; regen 45 HP/s after 5 s without dealing/taking damage, **not** while outside
  the zone; zone damage ignores armor. Rear hits x1.5 (outside 180° front arc around aim yaw).
- Vision: omnidirectional only within 45 m (LOS needed; buildings block), otherwise only inside the
  120° yaw wedge out to 200 m. **Sound**: walking <= 5 m/s is silent; steps are audible only 40 m;
  gun fire 320 m, mortar 420 m, laser 380 m, boost 90 m. So a still, non-shooting mech behind cover
  is nearly undetectable.
- Weapons: gun 45 dmg @ 252 m/s, 0.24 s, 12-round mag. Laser 315 hitscan, 0.75 s charge, telegraphed
  (`charging`, sound). Lightning 60 dmg, 45 m, 30° cone. Mortar 200+160 splash. Triangle: laser >
  mortar > lightning > laser. Weapons are loot only; 40 floor spawns + crates + dead mechs.
- Cubes: +4% max HP and +4% dmg each (cap 10), **+5% body size each (uncapped)** — a bigger mech is a
  bigger target. Upgrade offers at 1, 3, 6, 10, ... cubes.
- Supply drops at 70/140/210 s (land +25 s): stand alone in the 8 m ring 4 s → +3 cubes, **full HP,
  full energy**. A second mech in the ring freezes the clock.
- NPC drones: 12, 350 HP, weak guns (25 dmg/s), drop 2 cubes, leashed to home.

## 3. What conventional competitors probably do [H]

- Kit-style bot: walk to zone centre, grab a weapon, shoot the nearest mech (the baseline: 1.15).
  Fighting everything early means dying to a focused enemy or to the 20-260 dps zone when stuck
  behind buildings.
- Stronger teams (scores 4-5): add pathfinding on `world.json`, kiting, laser/lightning loot,
  upgrade selection, zone-aware movement. Most are likely to be aggressive "kill for cubes" bots
  because the brief pushes kills as a source of cubes/HP.
- Nobody can test combat locally (no simulator) so highly tuned aggression is hard to validate;
  survival heuristics are easier to reason about from the spec.

## 4. Legitimate edges the rules reward [H unless noted]

1. **Zero-zone-damage rule** [V derived]: always be inside `next_circle`. Pathfinding over real
   buildings so we never get trapped behind the stadium while the zone closes.
2. **Concealment** [V rules]: 45 m omni sight + 120° wedge beyond; walking <= 5 m/s silent; no firing
   unless attacked. Choose the standing point inside the next circle whose *line-of-sight openness*
   (computed from the real building footprints) is smallest, and sit still there.
3. **Regeneration** [V rules]: 45 HP/s after 5 s quiet. Breaking contact and hiding refills 1400 HP in
   ~31 s. Disengage-to-cover when hurt is worth more than trading shots.
4. **Rear-armor discipline** [V]: torso always faces the attacker (legs move independently) so we
   never eat the x1.5.
5. **Telegraph dodging**: `charging` + aim line = laser warning (0.75 s to sidestep); mortar shells
   are ballistic and predictable (gravity 22) → leave the impact point.
6. **Never carry a mortar** (laser cook-off +60%, lightning short, mortar loses to the laser carrier);
   prefer laser/lightning as the second slot only when it is a short detour.
7. **Supply drop** only when nobody else is near: +3 cubes (2 upgrade offers) and full HP/energy.
8. Placement, not kills: kills only for self-defence or finishing weak targets; cubes are a bonus.

## 5. Open questions / to measure with the first official run

- How strong are the built-in bots (aggressive hunters vs wanderers)? Official score vs the 1.15
  baseline and ~5.2 leader will tell.
- Does the "hold position" hide-spot choice beat simply standing near the next centre? (A/B across
  submissions, rate-limited.)
- Do final ("new seeds, other competitors") rankings reward the same behaviour? Unknown.
- Roof camping (jets: ~60 m climb on 100 energy) could be a further unconventional edge — not
  attempted in v1 because it cannot be validated without a simulator.

## 6. Environment notes (this session)

- `joinbuilders.org` is on the container's egress block list (403 from the proxy), so the kit
  archive had to be read through the in-app browser (`fetch` + `DecompressionStream`); the 1.5 MB
  `terrain.i16` cannot be relayed through tool output, so a local copy of the kit must be placed on the
  user's disk to run the real checker and test on the real map.
- The container's `GH_TOKEN` is invalid and no repositories are connected, so pushing to GitHub needs a
  user-provided credential or manual push.

## 7. Update — live track page read in the built-in browser (Sat 2026-11-21, ~01:15 UTC) [V = verified]

Source: https://www.joinbuilders.org/buildathon/tracks/builders (the slug is `builders`, **not** `mech-royale`, which 404s).

- [V] **Practice is not a free-for-all.** 48 matches against 15 house bots (4 easy, 4 medium, 4 hard, 3 expert) "acting as
  one squad": they share vision, never fight each other, and hunt *you* weakest-first: one early, more as you thin them
  out, all of them late. Practice seeds are private and fixed; your slot and spawn vary per match.
  Consequence: points = number of mechs that died before you, and every house bot is hunting you, so hiding alone is not
  enough once the squad converges. Survival early, being able to hold your own against one hunter, and not dying to the zone
  are what move the practice score. The qualifying round and the final are free-for-alls against other teams' bots.
- [V] **Submissions:** 4 per hour per team, scoring takes "a few minutes"; failed builds/checks are rejected with the reason and
  "never affect your best score"; a bot that crashes or stops answering stays in matches as a sitting target and is scored.
  The leaderboard shows each team's best practice score next to the starter bot.
- [V] **Sign-in is required to submit** (the form sits behind "Sign in" on the track page).
- [V] Cutoff: **Sun 11:00 AM EST** (= 16:00 UTC Sunday 2026-11-22). Chosen submission locks with its practice score
  (best unless another is picked). Top 32 -> qualifying (16 matches each vs other qualifiers) -> top 16 -> 64-match series.
  The repo must stay public with every submitted commit until results are published, or the entry scores nothing.
  Builds run linux/amd64 with network, <= 10 min; "pin versions because finalists are rebuilt" (this bot has no dependencies,
  only the `python:3.13-slim` base image).
- [V] Leaderboard snapshot, 74 teams: best practice scores 5.19 (two teams), 5.17, 4.73, 4.69, 4.63; most teams sit between 2.6
  and 4.4; starter baseline 1.15. A 5+ is the realistic target to beat.
- [V] Footsteps are only audible when a grounded mech moves faster than `sound.step_speed` = 5 m/s (range 40 m, every 0.6 s);
  walking slowly or standing still is silent (protocol.md section 8, config.sound). Used for "stealth walking" in ghost-v1.

### Implemented in ghost-v1 as a result [bot behaviour, measured in the simulator only]
- Zone-aware path cost + a hard "stay inside" re-plan when the cheapest route would leave the circle for >12 m.
- Hide spots restricted to the largest connected region of the *next* circle (all later circles nest inside it).
- Stealth walking (<= 4.7 m/s) while calm and able to arrive before the next shrink; sprint otherwise.
- NOT yet tuned against the squad-hunt format: combat is deliberately conservative (self-defence, finishing blows, flee < 35% HP).
  The first official run is the measurement of whether that is too passive or about right.
