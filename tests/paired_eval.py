#!/usr/bin/env python3
"""Paired local comparison of bot configurations over fixed seeds (uses arena_sim.py; the sim is NOT the official game).

    python3 tests/paired_eval.py --seeds 1-24 --know sense --diff 2 \
        base=ghost_bot_v2 v3=ghost_bot v3_noeff=ghost_bot:FARM_EFF=false ...

Each spec is NAME=BOT[:KEY=VAL,KEY=VAL]. The first spec is the baseline; the report gives mean placement points, the standard
error, and the paired difference (same seeds) with its standard error, plus kills / survival time / damage ratios."""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _src_hash(bot):
    h = hashlib.sha1()
    for d in ("../src", "../baseline", ".."):
        f = os.path.join(HERE, d, bot + ".py")
        if os.path.exists(f):
            h.update(open(f, "rb").read())
            break
    for f in ("nav.py", "arena_sim.py"):
        for d in ("../src", "."):
            q = os.path.join(HERE, d, f)
            if os.path.exists(q):
                h.update(open(q, "rb").read())
                break
    return h.hexdigest()[:12]


def run(spec, seeds, know, diff, procs, seconds):
    """Runs (or loads from the cache in /tmp/pe_cache) one configuration. The cache key includes the bot source hash."""
    name, rhs = spec.split("=", 1)
    bot, _, cfg = rhs.partition(":")
    os.makedirs("/tmp/pe_cache", exist_ok=True)
    key = hashlib.sha1(f"{bot}|{cfg}|{seeds}|{know}|{diff}|{seconds}|{_src_hash(bot)}".encode()).hexdigest()[:16]
    cache = f"/tmp/pe_cache/{key}.json"
    if os.path.exists(cache):
        return name, {r["seed"]: r for r in json.load(open(cache))}
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = [sys.executable, os.path.join(HERE, "arena_sim.py"), "--bot", bot, "--seeds", seeds, "--know", know,
           "--diff", str(diff), "--procs", str(procs), "--json", path]
    if seconds:
        cmd += ["--seconds", str(seconds)]
    if cfg:
        cmd += ["--cfg", cfg]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = json.load(open(path))
    os.unlink(path)
    for r in rows:
        r.pop("trace", None)
    json.dump(rows, open(cache, "w"))
    print(f"[done] {name} ({len(rows)} matches)", file=sys.stderr, flush=True)
    return name, {r["seed"]: r for r in rows}


def mean(v):
    return sum(v) / max(len(v), 1)


def se(v):
    if len(v) < 2:
        return float("nan")
    m = mean(v)
    return math.sqrt(sum((a - m) ** 2 for a in v) / (len(v) - 1) / len(v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1-24")
    ap.add_argument("--know", default="sense")
    ap.add_argument("--diff", type=float, default=2.0)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("specs", nargs="+")
    a = ap.parse_args()
    res = [run(s, a.seeds, a.know, a.diff, a.procs, a.seconds) for s in a.specs]
    base_name, base = res[0]
    lines = [f"know={a.know} diff={a.diff} seeds={a.seeds} (n={len(base)})",
             "name\tpoints(mean±se)\tΔpoints vs base (paired, ±se)\tkills\tsurvive_s\tdealt/taken\tacc\tnpc_kills\tcubes\terrors"]
    for name, rows in res:
        seeds = sorted(set(rows) & set(base))
        pts = [rows[s]["points"] for s in seeds]
        d = [rows[s]["points"] - base[s]["points"] for s in seeds]
        dt = sum(rows[s]["dealt"] for s in seeds) / max(1.0, sum(rows[s]["taken"] for s in seeds))
        lines.append("%s\t%.2f±%.2f\t%+.2f±%.2f\t%.2f\t%.0f\t%.2f\t%.3f\t%.2f\t%.1f\t%d" % (
            name, mean(pts), se(pts), mean(d), se(d) if name != base_name else 0.0, mean([rows[s]["kills"] for s in seeds]),
            mean([rows[s]["t"] for s in seeds]), dt, mean([rows[s]["acc"] for s in seeds]),
            mean([rows[s]["npc_kills"] for s in seeds]), mean([rows[s]["cubes"] for s in seeds]),
            sum(rows[s]["errors"] for s in seeds)))
    txt = "\n".join(lines)
    print(txt)
    if a.out:
        with open(a.out, "a") as f:
            f.write(txt + "\n\n")


if __name__ == "__main__":
    main()
