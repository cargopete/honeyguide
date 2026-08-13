#!/usr/bin/env python3
"""Q2: which overlay mechanism, and what does the compile gate cost inside it.

RFC-0001 §6.2 says edits land in an overlay and `cargo check` decides. §15 Q2
leaves open what the overlay actually is. The candidates differ on three axes
and only one of them is obvious:

  - **Cost to build.** Paid once per session, so a second or two is fine.
  - **Aliasing.** A hardlink forest shares inodes with the working tree, so any
    writer that truncates in place edits the user's real files. That is a
    data-loss bug wearing a performance improvement's clothes.
  - **Whether cargo can still be fast in it.** This is the one that decides the
    design, and it is not guessable. Cargo fingerprints record absolute paths,
    so building the same crate from a second directory may invalidate
    everything, and two source paths sharing one `--target-dir` may thrash each
    other's cache on every alternation. If that happens, the gate goes from
    sub-second to a full rebuild every turn and §6.2's central claim dies.

Method follows §12.1: three trials of every timing, all of them reported, and a
behavioural check wherever a behavioural check is available, since those are the
findings that reproduce.

Usage:
    python3 scripts/overlay-probe.py --repo ~/Projects/dipper --work /tmp/hg-q2
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

GATE = "cargo check --workspace --all-targets"

# A change that compiles and cascades: a new public method on a type in the core
# crate, which forces every dependent crate to be re-checked. Not a change that
# *breaks*, because a broken build stops at the first error and reports a
# flattering fraction of the real work.
ANCHOR = "    /// Total documents in the catalogue."
CASCADE = "    pub fn hg_probe_%d(&self) -> usize { 0 }\n\n" + ANCHOR
TARGET_FILE = "crates/dipper-index/src/lib.rs"


def sh(cmd, cwd, env=None, quiet=True):
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=quiet, text=True,
                       env=env)
    return p.returncode, time.time() - t0, (p.stderr or "") if quiet else ""


def gate(cwd, target_dir, trials=3, cascade=False):
    """Time the gate. With cascade, edit the core crate first so the check has
    real work to do, and restore afterwards."""
    env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir))
    out = []
    for i in range(trials):
        f = Path(cwd) / TARGET_FILE
        original = f.read_text() if cascade else None
        if cascade:
            f.write_text(original.replace(ANCHOR, CASCADE % i, 1))
        rc, secs, _ = sh(GATE, cwd, env)
        out.append(round(secs, 2))
        if cascade:
            f.write_text(original)
            sh(GATE, cwd, env)          # settle, so the next trial starts warm
        if rc != 0:
            out[-1] = f"rc={rc}"
    return out


def du_mb(path):
    p = subprocess.run(f"du -sm {path}", shell=True, capture_output=True, text=True)
    return int(p.stdout.split()[0]) if p.stdout.strip() else -1


def aliases(repo, overlay):
    """Behavioural, not theoretical: write to the overlay the way a naive tool
    writes (open, truncate, write) and see whether the working tree moved."""
    src = Path(repo) / TARGET_FILE
    dst = Path(overlay) / TARGET_FILE
    before = src.read_text()
    with open(dst, "w") as fh:
        fh.write(before + "\n// hg overlay aliasing probe\n")
    after = src.read_text()
    dst.write_text(before)
    return after != before


MECHANISMS = {
    "copy":     "cp -R {src}/. {dst}",
    "clone":    "cp -Rc {src}/. {dst}",        # APFS clonefile, copy-on-write
    "hardlink": "cp -Rl {src}/. {dst}",
    "worktree": "git -C {src} worktree add --detach {dst} HEAD",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--work", default="/tmp/hg-q2")
    ap.add_argument("--trials", type=int, default=3)
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shared = work / "shared-target"

    print(f"repo={repo}  work={work}\nwarming the working tree's own gate ...", flush=True)
    sh(GATE, repo, dict(os.environ, CARGO_TARGET_DIR=str(shared)))

    report = {"repo": str(repo), "gate": GATE, "mechanisms": {}}
    report["baseline"] = {
        "noop": gate(repo, shared, a.trials),
        "cascade": gate(repo, shared, a.trials, cascade=True),
    }
    print("baseline (working tree, its own warm target dir):",
          json.dumps(report["baseline"]), flush=True)

    for name, template in MECHANISMS.items():
        dst = work / name
        if dst.exists():
            sh(f"git -C {repo} worktree remove --force {dst}", ".")
            shutil.rmtree(dst, ignore_errors=True)
        if name != "worktree":
            dst.mkdir(parents=True)

        rc, build_s, err = sh(template.format(src=repo, dst=dst), ".")
        if rc != 0:
            print(f"{name:<9} unavailable: {err.strip()[:120]}")
            report["mechanisms"][name] = {"error": err.strip()[:200]}
            continue

        own = work / f"target-{name}"
        r = {
            "build_s": round(build_s, 2),
            "disk_mb": du_mb(dst),
            "aliases_working_tree": aliases(repo, dst),
            # Its own target dir: cold once, then warm.
            "own_target_cold_s": gate(dst, own, 1)[0],
            "own_target_noop": gate(dst, own, a.trials),
            "own_target_cascade": gate(dst, own, a.trials, cascade=True),
            # The interesting one. Does sharing a target dir with the working
            # tree cost a rebuild, and does alternating between the two thrash?
            "shared_target_first": gate(dst, shared, 1)[0],
            "shared_target_noop": gate(dst, shared, a.trials),
            "shared_alternating": [gate(dst, shared, 1)[0], gate(repo, shared, 1)[0],
                                   gate(dst, shared, 1)[0], gate(repo, shared, 1)[0]],
        }
        report["mechanisms"][name] = r
        print(f"{name:<9} {json.dumps(r)}", flush=True)

    # `git worktree add` registers itself in the target repo's metadata, so
    # leaving it behind pollutes a repository this script does not own.
    sh(f"git -C {repo} worktree remove --force {work / 'worktree'}", ".")
    sh(f"git -C {repo} worktree prune", ".")

    out = work / "overlay-probe.json"
    out.write_text(json.dumps(report, indent=1))
    print(f"\nwritten to {out}")
    print("\nthe number that decides §6.2: `shared_alternating`. If those four "
          "figures stay sub-second, one warm target directory serves both trees "
          "and the gate stays cheap. If they climb, each tree needs its own and "
          "the session pays one cold build at startup.")


if __name__ == "__main__":
    raise SystemExit(main())
