# Experiment ledger — official scores vs exact code

Rule: one row per official submission. A score is only entered with the source named. The official practice score is the
mean over 48 matches against the 15 coordinated house bots; its standard error is about 0.4, so differences under about 0.8
between two single runs are not evidence. Organizers keep the best practice score unless another submission is selected.

## Submissions so far

| # | Bot | Commit (repo `akshayagg18/mech-royale`, `main`) | Config | Official practice score / rank | Source |
| --- | --- | --- | --- | --- | --- |
| S1 | ghost-v1 | `e3fbf795f27857c141b6b06eb2e054e88af9b5f7` | n/a (no `CFG`) | **2.48, rank 77 of 103** at submission (leader 5.25, baseline 1.15, average place about 13.52 of 16) | Reported by the user in the mission brief of Sat 2026-11-21 10:44 SGT. I did not make this submission and hold no receipt or submission id. |
| S2 | ghost-v2 | filled in below once pushed | defaults in `ghost_bot.CFG` (FARM on, OPP off, RECOVER on, DROP_REACH 260, SOUND_AIM on) | **not submitted by me; no official result** | see the report |

The commit hash for S2 is recorded in `FIRST_SUBMISSION_REPORT.md` (section "What to publish") and in the final message; it
cannot be written into the commit it describes.

## What changed between S1 and S2 (each line is a separate `CFG` flag unless noted)

| Change | Flag / code | Default |
| --- | --- | --- |
| Break crates, shoot drones, collect cubes and weapons while calm | `FARM`, `FARM_UNTIL = 200`, `FARM_HP = 0.55`, `FARM_STAND = 30` | on |
| Opportunistic engagement of favourable targets | `OPP`, `OPP_RANGE = 105`, `OPP_MAXN = 2`, `FAVOR = 0.9` | **off** |
| Provoked / close fights chosen by relative kill time, range by loadout | `CLOSE = 55`, `_choose_fight`, `_pref_range` | on (not switchable) |
| Retreat threshold 28% + 7% per extra close enemy; recover vs flee | `RETREAT = 0.28`, `RECOVER`, `RECOVER_RANGE = 90` | on |
| Zone push inside duels | `ZONE_BUF = 14` | on (0 = push only once already outside the circle) |
| Supply-drop reach 110 m -> 260 m | `DROP_REACH = 260` | on |
| Face gunfire when standing still | `SOUND_AIM` | on |
| Upgrade order: damage and survivability first | `UPGRADE_ORDER` | on (not switchable) |

## Planned official runs (change one thing per run; 4 submissions per hour; read the result before the next)

| Run | Change vs the previous best | Hypothesis | Expected effect | Decide |
| --- | --- | --- | --- | --- |
| S2 | all of the above vs S1 | v1 was weak because it never collected anything and fought only on the squad's terms | +0.5 to +2 (uncertain) | baseline for the rest |
| S3 | `OPP = True` | taking fights that we win on kill time reduces the squad's initiative | -0.5 to +1; the simulator says no gain | keep only if > +0.8 over S2 |
| S4 | `FARM_UNTIL = 120` or `FARM = False` (whichever the S2 result argues for) | farming exposes us early | if S2 < S1: expect S4 > S2 | isolates the farming effect |
| S5 | `RECOVER = False` | the recovery state wastes damage windows | small | revert if worse |

Each row should name a commit; a `CFG` change is a one-line commit so the diff between rows is exactly one flag.

## How to read a result

* S2 above S1 by more than 0.8: the collection side (RC-1..3) works; next is S3 (engagement).
* S2 within 0.8 of S1: no evidence either way; do not conclude. Run S4 to see whether farming hurts or helps.
* S2 below S1 by more than 0.8: farming or the new fight logic is a net loss; revert flags one at a time, `FARM` first.
