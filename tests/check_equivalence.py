#!/usr/bin/env python3
"""Ghost-v2 must stay recoverable: the v3 bot with PRESET="v2" (all v3 flags off) has to reproduce baseline/ghost_bot_v2.py
decision for decision. This runs both in the squad-hunt simulator on fixed seeds and requires identical per-seed results
(the simulator is deterministic).

    python3 tests/check_equivalence.py [--seeds 1-8] [--know sense] [--diff 2]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from paired_eval import run  # noqa: E402

V2 = ("AIM_SPLIT=false,TURN_AWARE=false,LASER_HOLD=false,TRUST_OBS=false,DODGE_GUN=false,FARM_EFF=false,FARM_PLAN=false,"
      "QUIET_RETREAT=false,QUIET_FARM=false,THREAT_FARM=false,THREAT_HIDE=false,ENGAGE=off,SCAN360=false,UPG=damage")

ap = argparse.ArgumentParser()
ap.add_argument("--seeds", default="1-8")
ap.add_argument("--know", default="sense")
ap.add_argument("--diff", type=float, default=2.0)
a = ap.parse_args()
_, base = run("base=ghost_bot_v2", a.seeds, a.know, a.diff, 2, 0)
_, off = run("v3off=ghost_bot:" + V2, a.seeds, a.know, a.diff, 2, 0)
bad = [s for s in base if any(base[s][k] != off[s][k] for k in ("points", "place", "kills", "dealt", "taken", "t", "cubes"))]
print("seeds compared: %d  mismatches: %s" % (len(base), bad or "none"))
sys.exit(1 if bad else 0)
