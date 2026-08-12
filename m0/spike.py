#!/usr/bin/env python3
"""M0: the thesis spike. Throwaway by design, per RFC-0001 §13.

This is not the harness. It is the smallest thing that can answer the only
question that matters before building one: **when a ~3B-active local model is
handed a map and made to work behind a compile gate, how often does its first
proposed edit actually compile?**

It implements the parts of RFC-0001 that the answer depends on, and nothing else:

  §6.1  five tools, hard ceiling
  §6.2  the compile gate: edits land in an overlay, cargo check decides
  §6.3  deterministic preconditions, each costing zero model tokens:
          read-before-edit, no-stalling, missing-args, oversize-edit
  §7    search/replace matched whitespace-exact and uniquely, no fuzzy apply
        (plus gutter normalisation, undoing our own line numbering)

Metrics, printed at the end:

  WFA   well-formed action rate. Should be ~100% under schema constraint;
        anything less is a serving bug, not model drift.
  FACP  first-apply cargo check pass rate. The real number. M0 gate is 60%.

Usage:
    python3 m0/spike.py --repo /path/to/dipper-worktree [--phases 2] [--task rename]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST = os.environ.get("HG_OLLAMA", "http://pepe-thinkpad:11434")
MODEL = os.environ.get("HG_MODEL", "heretic:latest")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SYSTEM = (REPO_ROOT / "prompts" / "system.md").read_text()
SCHEMA = json.loads((REPO_ROOT / "prompts" / "action-schema.json").read_text())

MAX_TURNS = 12
MAX_EDIT_LINES = 200
OBS_CAP = 4000          # characters; observations are truncated hard (§8.5)
GUTTER = re.compile(r"^\s*\d+\|", re.M)


# ---------------------------------------------------------------- model access

def _post(payload, timeout=1800):
    req = urllib.request.Request(HOST + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def call(messages, schema=None, num_predict=600):
    payload = {
        "model": MODEL, "messages": messages, "stream": False, "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.3, "top_p": 0.95, "top_k": 20, "min_p": 0.05,
                    "num_ctx": 32768, "num_predict": num_predict},
    }
    if schema:
        payload["format"] = schema
    try:
        return _post(payload)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        payload.pop("think", None)
        return _post(payload)


# ---------------------------------------------------------------------- tools

class Session:
    """One task attempt against one overlay."""

    def __init__(self, repo: Path, target_dir: Path, check_cmd):
        self.repo = repo
        self.env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir))
        self.check_cmd = check_cmd
        self.read_paths = set()
        self.last_action = None
        self.stalls = 0
        self.edits_attempted = 0
        self.edits_first_apply_ok = 0
        self.refusals = []

    # -- helpers

    def _abs(self, path):
        p = (self.repo / path).resolve()
        if not str(p).startswith(str(self.repo.resolve())):
            return None
        return p

    def run_check(self):
        t0 = time.time()
        p = subprocess.run(self.check_cmd, cwd=self.repo, env=self.env,
                           capture_output=True, text=True, shell=True)
        return p.returncode == 0, (p.stderr or p.stdout), time.time() - t0

    # -- the five tools

    def read(self, path, start=None, end=None):
        p = self._abs(path)
        if p is None or not p.is_file():
            return self.refuse("bad_path", f"no such file: {path}")
        lines = p.read_text(errors="replace").splitlines()
        s = max(1, start or 1)
        e = min(len(lines), end or min(len(lines), s + 120))
        self.read_paths.add(str(p))
        body = "\n".join(f"{i:>4}| {lines[i-1]}" for i in range(s, e + 1))
        return f"read {path} lines {s}-{e} of {len(lines)}\n{body}"[:OBS_CAP]

    def search(self, query):
        # Stands in for scip.sqlite until hg-index exists. Structural enough for
        # a spike: symbol-ish grep, capped at 20 hits. Falls back to grep because
        # ripgrep is not always a real binary on the PATH.
        import shutil
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-m", "20", query, "crates"]
        else:
            cmd = ["grep", "-rn", "--include=*.rs", query, "crates"]
        p = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True)
        hits = (p.stdout or "").strip().splitlines()[:20]
        if not hits:
            return f"search {query!r}: no hits"
        return f"search {query!r}: {len(hits)} hits\n" + "\n".join(hits)[:OBS_CAP]

    def edit(self, path, search, replace):
        p = self._abs(path)
        if p is None or not p.is_file():
            return self.refuse("bad_path", f"no such file: {path}")

        # §6.3, revised by M0. Refusing an edit on an unread path is correct but
        # useless: the model does not go and read, it repeats the edit. So satisfy
        # the precondition instead of punishing it. The edit is still not applied,
        # because the search text was written blind, but the turn now produces the
        # thing the model needed rather than a scolding.
        if str(p) not in self.read_paths:
            self.refusals.append("path_not_read_satisfied")
            body = self.read(path)
            return ("Your edit was not applied: you had not read this file, so the "
                    "`search` text was guesswork. Here is the file. Copy `search` "
                    "exactly from it and edit again.\n\n" + body)

        # §7 undo our own line-number gutter before matching. Not fuzzy matching:
        # normalising a transformation we introduced.
        if search and all(GUTTER.match(l) for l in search.splitlines() if l.strip()):
            search = GUTTER.sub("", search)
            if search.startswith(" "):
                search = "\n".join(l[1:] if l.startswith(" ") else l for l in search.splitlines())

        if replace.count("\n") + 1 > MAX_EDIT_LINES:
            return self.refuse("edit_too_large", "replacement is over 200 lines; split it")

        text = p.read_text()
        n = text.count(search)
        if n == 0:
            near = search.strip().splitlines()[0][:60] if search.strip() else ""
            hint = ""
            for i, line in enumerate(text.splitlines(), 1):
                if near and near.strip() in line:
                    hint = f" nearest similar line is {i}: {line.strip()[:80]!r}"
                    break
            return self.refuse("search_not_found",
                               f"that exact text is not in {path}.{hint}")
        if n > 1:
            return self.refuse("search_ambiguous",
                               f"that text appears {n} times in {path}; extend it to be unique")

        # gate
        self.edits_attempted += 1
        before = text
        p.write_text(text.replace(search, replace, 1))
        ok, diag, secs = self.run_check()
        if ok:
            self.edits_first_apply_ok += 1
            return f"edit applied to {path} and `cargo check` passed in {secs:.1f}s"
        p.write_text(before)          # bounce: overlay never keeps a broken edit
        return f"edit REJECTED, `cargo check` failed and the change was reverted:\n{diag[-2500:]}"

    def check(self):
        ok, diag, secs = self.run_check()
        return f"cargo check {'passed' if ok else 'FAILED'} in {secs:.1f}s\n{diag[-2500:]}"

    def refuse(self, reason, detail):
        self.refusals.append(reason)
        return f"REFUSED ({reason}): {detail}"

    # -- dispatch with preconditions

    REQUIRED = {"read": ["path"], "search": ["query"], "edit": ["path", "search", "replace"],
                "check": [], "finish": ["summary"]}

    def dispatch(self, a):
        tool = a.get("tool")
        if tool not in self.REQUIRED:
            return self.refuse("unknown_tool", f"{tool!r} is not a tool")

        missing = [f for f in self.REQUIRED[tool] if not (a.get(f) or "").strip()]
        if missing:
            return self.refuse("missing_args",
                               f"`{tool}` needs {', '.join(missing)}; you sent none of them")

        # §6.3 no stalling. The driver's measured failure mode is repetition.
        sig = (tool, a.get("path", ""), a.get("query", ""), a.get("search", ""))
        if sig == self.last_action:
            self.stalls += 1
            if self.stalls >= 3:
                return self.refuse("stalled_abort", "ABORT")
            return self.refuse("stalled",
                               f"you already did exactly this `{tool}` and got a result above. "
                               "Do not repeat it; your next action must be different.")
        self.stalls = 0
        self.last_action = sig

        if tool == "read":
            return self.read(a["path"], a.get("start"), a.get("end"))
        if tool == "search":
            return self.search(a["query"])
        if tool == "edit":
            return self.edit(a["path"], a["search"], a["replace"])
        if tool == "check":
            return self.check()
        return None  # finish


# ------------------------------------------------------------------ the loop

def run_task(task, repo, target_dir, phases, brief, verbose=True):
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, capture_output=True)
    if task.get("setup"):
        task["setup"](repo)

    s = s0 = Session(repo, target_dir, task.get("check_cmd", "cargo check --workspace"))
    # §5.3: retrieval pre-loads the bodies in the blast radius so the model does
    # not have to spend turns fetching what the harness already knows it needs.
    preload = ""
    for rel in task.get("preload", []):
        f = repo / rel
        if f.is_file():
            lines = f.read_text().splitlines()
            s0.read_paths.add(str(f.resolve()))
            preload += (f"\n\n{rel} ({len(lines)} lines):\n"
                        + "\n".join(f"{i:>4}| {l}" for i, l in enumerate(lines, 1)))

    msgs = [{"role": "system", "content": SYSTEM + "\n\n" + brief},
            {"role": "user", "content": "Task: " + task["prompt"] + preload}]

    wfa_ok = wfa_total = 0
    t0 = time.time()
    turns = 0
    for turns in range(1, MAX_TURNS + 1):
        if phases == 2:
            # §6 phase 1: unconstrained reasoning, cheap because the prefix is cached
            r = call(msgs + [{"role": "user",
                              "content": "In one or two sentences, what is the single next action and why?"}],
                     num_predict=160)
            think = ((r.get("message") or {}).get("content") or "").strip()
            if verbose and think:
                print(f"    [{turns}] think: {think[:150]}")
            msgs.append({"role": "assistant", "content": think})

        wfa_total += 1
        r = call(msgs, schema=SCHEMA, num_predict=900)
        raw = (r.get("message") or {}).get("content") or ""
        try:
            a = json.loads(raw)
            wfa_ok += 1
        except Exception:
            msgs.append({"role": "user", "content": "That was not valid JSON. Emit one action object."})
            continue

        msgs.append({"role": "assistant", "content": raw})
        if verbose:
            print(f"    [{turns}] {a.get('tool')} {a.get('path') or a.get('query') or ''}"[:110])

        if a.get("tool") == "finish" and (a.get("summary") or "").strip():
            break
        obs = s.dispatch(a)
        if obs is None:
            break
        if obs.endswith("ABORT"):
            if verbose:
                print("          -> aborting: stalled three times on the same action")
            break
        if verbose:
            print(f"          -> {obs.splitlines()[0][:110]}")
        msgs.append({"role": "user", "content": obs[:OBS_CAP]})

    wall = time.time() - t0
    ok, diag, _ = s.run_check()
    oracle_ok = ok
    if task.get("oracle_cmd"):
        p = subprocess.run(task["oracle_cmd"], cwd=repo, env=s.env,
                           capture_output=True, text=True, shell=True)
        oracle_ok = p.returncode == 0

    return {
        "task": task["name"], "turns": turns, "wall_s": round(wall, 1),
        "wfa": (wfa_ok, wfa_total),
        "edits": s.edits_attempted, "edits_ok_first_apply": s.edits_first_apply_ok,
        "final_check_ok": ok, "oracle_ok": oracle_ok,
        "refusals": s.refusals,
    }


# -------------------------------------------------------------------- tasks

def plant_failing_test(repo):
    p = repo / "crates/dipper-index/src/lib.rs"
    t = p.read_text()
    assert "Ok(self.len()? == 0)" in t
    p.write_text(t.replace("Ok(self.len()? == 0)", "Ok(self.len()? == 1)", 1))


IDX = "crates/dipper-index/src/lib.rs"

TASKS = [
    {"name": "rename", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, rename the public method `count` on "
               "`Catalogue` to `matches`. Update every caller in that file too."},
    {"name": "add_field", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, add a new public field "
               "`pub language: Option<String>,` to the `Record` struct, after the `creator` field."},
    {"name": "fix_test", "preload": [IDX],
     "prompt": "The test suite in crates/dipper-index is failing. Find the bug in "
               "`Catalogue::is_empty` in crates/dipper-index/src/lib.rs and fix it.",
     "setup": plant_failing_test,
     "oracle_cmd": "cargo test -p dipper-index --quiet"},
    {"name": "display_impl", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, add an `impl std::fmt::Display for Hit` "
               "that writes the record's identifier followed by the score in parentheses, "
               "for example `nasa-apollo (12.5)`."},
    {"name": "doc_comment", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, the method `Catalogue::is_empty` has no "
               "doc comment. Add a one-line `///` doc comment above it describing what it returns."},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--target-dir", default="/tmp/hg-m0-target")
    ap.add_argument("--phases", type=int, default=1, choices=[1, 2])
    ap.add_argument("--task", default=None)
    ap.add_argument("--brief", default=str(HERE / "dipper-brief.md"))
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    brief = Path(a.brief).read_text() if Path(a.brief).exists() else ""
    tasks = [t for t in TASKS if a.task in (None, t["name"])]

    print(f"model={MODEL} host={HOST} phases={a.phases} repo={repo}")
    print(f"warming cargo cache ...", flush=True)
    subprocess.run("cargo check --workspace", cwd=repo, shell=True, capture_output=True,
                   env=dict(os.environ, CARGO_TARGET_DIR=a.target_dir))

    results = []
    for t in tasks:
        print(f"\n=== {t['name']} ===", flush=True)
        results.append(run_task(t, repo, Path(a.target_dir), a.phases, brief))
        print("   ", json.dumps(results[-1]), flush=True)

    ea = sum(r["edits"] for r in results)
    eo = sum(r["edits_ok_first_apply"] for r in results)
    wo = sum(r["wfa"][0] for r in results)
    wt = sum(r["wfa"][1] for r in results)
    print("\n" + "=" * 66)
    print(f"WFA   {wo}/{wt} = {100*wo/max(wt,1):.0f}%")
    print(f"FACP  {eo}/{ea} = {100*eo/max(ea,1):.0f}%   (M0 gate: 60%)")
    print(f"oracle passed  {sum(1 for r in results if r['oracle_ok'])}/{len(results)}")
    print(f"median turns   {sorted(r['turns'] for r in results)[len(results)//2]}")
    print(f"total wall     {sum(r['wall_s'] for r in results):.0f}s")
    print("=" * 66)
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    sys.exit(main())
