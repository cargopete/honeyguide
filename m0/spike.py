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
import collections
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
MAX_NO_PROGRESS = 3     # red gates without reducing the error count, then roll back
ERRLINE = re.compile(r"^error(\[[A-Z]\d+\])?: (.*)$", re.M)


def count_errors(diag):
    """Distinct rustc errors, ignoring cargo's own summary lines. The gate's
    rollback rule keys off whether this is falling, not off how many turns have
    been red: a change that cascades is red on every intermediate turn by
    construction, and counting turns punishes exactly the tasks the gate exists
    to make possible."""
    return sum(1 for m in ERRLINE.finditer(diag)
               if "could not compile" not in m.group(2)
               and "aborting due to" not in m.group(2))


# ---------------------------------------------------------------- model access

def _post(payload, timeout=420, retries=3):
    """Retries on a stalled socket. A trial-3 run died to a read timeout after
    two clean trials, with the host afterwards reachable but holding no model, so
    the ThinkPad went away rather than the script misbehaving. Losing a whole
    suite to one dropped connection is not a measurement property worth keeping.
    The retry is logged, because a silent retry would hide exactly the host
    instability §12.1 rule 6 says to record."""
    req = urllib.request.Request(HOST + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError:
            raise           # subclass of URLError; the 400 fallback in call() owns it
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt == retries:
                raise
            print(f"          !! {type(e).__name__} on attempt {attempt}, retrying", flush=True)
            time.sleep(15 * attempt)


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
        self.read_end = {}          # path -> highest line served, for paging
        self.recent = collections.deque(maxlen=4)
        self.stalls = 0
        # Three counters, not two. `edit_actions` is every edit the model
        # proposed; `edits_applied` is the subset whose `search` matched the file
        # uniquely and therefore reached the gate. Reporting FACP only over the
        # second denominator flatters a model that fabricates search text, since
        # a fabricated edit never gets counted against it.
        self.edit_actions = 0
        self.edits_applied = 0
        self.edits_first_apply_ok = 0
        self.refusals = []
        # §6.2, revised by M0. Edits accumulate; a red gate is a fact about the
        # overlay, not a reason to undo the work. `green` holds the contents of
        # every touched path as of the last green gate, so three consecutive reds
        # can be rolled back by the harness rather than by the model.
        self.gate_green = True
        self.green = {}
        self.no_progress = 0
        self.best_errors = None
        self.reverts = 0

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

    def read(self, path, start=None, end=None, advance=False):
        p = self._abs(path)
        if p is None or not p.is_file():
            return self.refuse("bad_path", f"no such file: {path}")
        lines = p.read_text(errors="replace").splitlines()
        s = max(1, start or 1)
        if advance:
            # M0 finding: a repeated identical `read` is a question the harness
            # can answer. Both models re-read a file they had already been shown,
            # and both were scolded for it. Page forward instead: same provision
            # principle as the unread-path branch of `edit`.
            s = self.read_end.get(str(p), 0) + 1
            end = None
            if s > len(lines):
                return self.refuse("read_exhausted",
                                   f"you have already been shown all {len(lines)} lines of "
                                   f"{path}. Reading it again will not help; edit it.")
        e = min(len(lines), end or min(len(lines), s + 120))
        self.read_paths.add(str(p))
        self.read_end[str(p)] = max(self.read_end.get(str(p), 0), e)
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
        self.edit_actions += 1
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

        # M0 finding, and it was inflating the headline metric. The model emits
        # edits whose `replace` is byte-identical to `search`. The harness applied
        # them, `cargo check` passed because nothing had changed, and FACP counted
        # a first-apply pass. The model then re-sent the same no-op, correctly,
        # because it could see the task was not done while the harness insisted
        # the edit had landed. An edit that changes nothing is not an edit.
        if search == replace:
            return self.refuse("edit_is_a_noop",
                               "`search` and `replace` are identical, so that edit changes "
                               "nothing. `replace` must be the new text.")

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
        self.edits_applied += 1
        self.green.setdefault(str(p), text)     # session starts green
        p.write_text(text.replace(search, replace, 1))
        ok, diag, secs = self.run_check()
        self.gate_green = ok
        if ok:
            self.edits_first_apply_ok += 1
            self.no_progress = 0
            self.best_errors = None
            for q in self.green:
                self.green[q] = Path(q).read_text()
            # M0 finding: a bare success reads as no signal at all. Both models
            # re-sent an edit that had already landed. Say what happens next.
            return (f"edit applied to {path} and `cargo check` passed in {secs:.1f}s. "
                    "Do not send this edit again. If the task is now complete, emit "
                    "`finish` with a one-line summary; otherwise make the next edit.")

        # Red. The edit stays. Some correct changes have no compiling
        # intermediate state (adding a struct field breaks every literal), and
        # reverting each attempt makes them unreachable: the model re-adds the
        # field, the harness removes it, forever.
        n = count_errors(diag)
        was = self.best_errors          # None means "green until now, no baseline"
        if was is None or n < was:
            self.best_errors = n
            self.no_progress = 0
        else:
            self.no_progress += 1

        if self.no_progress >= MAX_NO_PROGRESS:
            for q, t in self.green.items():
                Path(q).write_text(t)
            self.no_progress = 0
            self.best_errors = None
            self.gate_green = True
            self.reverts += 1
            return (f"`cargo check` has failed {MAX_NO_PROGRESS} turns running without the "
                    "error count going down, so the harness has rolled the files back to "
                    f"the last state that compiled. Start again from there.\n{diag[-2000:]}")

        trend = f"{n} error(s), down from {was}" if was and n < was else f"{n} error(s)"
        return (f"edit applied to {path}, but `cargo check` now FAILS with {trend}. The edit "
                "was KEPT. Fix these errors with your next edit; if they are the expected "
                "consequence of the change you just made, repair them one at a time.\n"
                + diag[-2500:])

    def check(self):
        ok, diag, secs = self.run_check()
        self.gate_green = ok
        if ok:
            self.no_progress = 0
            self.best_errors = None
            for q in self.green:
                self.green[q] = Path(q).read_text()
        return f"cargo check {'passed' if ok else 'FAILED'} in {secs:.1f}s\n{diag[-2500:]}"

    def refuse(self, reason, detail):
        self.refusals.append(reason)
        return f"REFUSED ({reason}): {detail}"

    # -- dispatch with preconditions

    REQUIRED = {"read": ["path"], "search": ["query"], "edit": ["path", "search", "replace"],
                "check": [], "finish": ["summary"]}

    # M0 finding: the stall signature must be scoped to the fields the tool in
    # hand actually uses. The schema forces every field on every action (see
    # prompts/README.md), so a `check` carries leftover `search` text that varies
    # turn to turn, and a signature over all fields silently stops matching. Two
    # consecutive identical `check`s were served before the rule fired.
    SIG_FIELDS = {"read": ("path", "start", "end"), "search": ("query",),
                  "edit": ("path", "search", "replace"), "check": (), "finish": ()}

    def dispatch(self, a):
        tool = a.get("tool")
        if tool not in self.REQUIRED:
            return self.refuse("unknown_tool", f"{tool!r} is not a tool")

        missing = [f for f in self.REQUIRED[tool] if not (a.get(f) or "").strip()]

        # §6.3 no stalling, and this check comes FIRST. When it ran after the
        # missing-args check, nothing was recorded for a malformed
        # action, so a model repeating the same malformed action was never seen
        # to be repeating: heretic sent the identical argument-less `edit` twelve
        # times and collected twelve separate refusals. A rule that only runs on
        # well-formed input is not a rule against stalling.
        # It also compares against the last few actions rather than only the
        # previous one. Refused an identical edit, the model alternates between
        # two near-identical malformed forms, and A-B-A-B defeats a detector with
        # a memory of one. Observed over turns 8 to 12 of a rename attempt.
        sig = (tool,) + tuple(str(a.get(f) or "") for f in self.SIG_FIELDS[tool])
        if sig in self.recent:
            if tool == "read" and not missing:
                obs = self.read(a["path"], a.get("start"), a.get("end"), advance=True)
                if not obs.startswith("REFUSED"):
                    self.stalls = 0
                    return obs
            self.stalls += 1
            if self.stalls >= 3:
                return self.refuse("stalled_abort", "ABORT")
            return self.refuse("stalled",
                               f"you already did this `{tool}` and got a result above. "
                               "Do not repeat it; your next action must be different.")
        self.stalls = 0
        self.recent.append(sig)

        if missing:
            return self.refuse("missing_args",
                               f"`{tool}` needs {', '.join(missing)}; you sent none of them")

        if tool == "read":
            return self.read(a["path"], a.get("start"), a.get("end"))
        if tool == "search":
            return self.search(a["query"])
        if tool == "edit":
            return self.edit(a["path"], a["search"], a["replace"])
        if tool == "check":
            return self.check()
        # finish. M0 finding: the control model searched, read, checked and then
        # declared the task complete having made no edit at all, with every
        # action well-formed and permitted. Nothing in §6.3 caught it, because
        # nothing about it was malformed. The harness knows whether an edit ever
        # passed the gate, so it can simply decline to believe the claim.
        if self.edits_applied == 0:
            return self.refuse("finish_without_edit",
                               "you have not edited anything in this session, so the task is "
                               "not done. Send the edit.")
        if not self.gate_green:
            return self.refuse("finish_while_red",
                               "the overlay does not compile, so the task is not done. "
                               "Fix the errors from the last `cargo check` first.")
        return None


# ------------------------------------------------------------------ the loop

def run_task(task, repo, target_dir, phases, brief, verbose=True, transcript=None):
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, capture_output=True)
    if task.get("setup"):
        task["setup"](repo)

    # M0 finding: plain `cargo check` does not typecheck `#[cfg(test)]` code, and
    # dipper's callers of the renamed method live in a `mod tests` in the same
    # file. Without --all-targets the gate is blind to exactly what the rename
    # task asks the model to change, so an edit can pass the gate and fail the
    # oracle. §6.2 says the gate is the mechanism the design leans on; it has to
    # see the whole crate.
    s = s0 = Session(repo, target_dir,
                     task.get("check_cmd", "cargo check --workspace --all-targets"))
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

    # §4.1: the event log is the transcript, the telemetry and the eval corpus,
    # and is a first-class artifact from day one. Its absence is why the first
    # heretic run could not say *why* an edit was refused.
    def rec(**kw):
        if transcript:
            with open(transcript, "a") as fh:
                fh.write(json.dumps(kw) + "\n")

    rec(kind="task", task=task["name"], model=MODEL, prompt=task["prompt"],
        preload_lines=preload.count("\n"), brief=bool(brief))

    wfa_ok = wfa_total = 0
    t0 = time.time()
    turns = 0
    turn_s = []          # §13's second gate is a *median turn*, so time each one
    for turns in range(1, MAX_TURNS + 1):
        t_turn = time.time()
        try:
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
                rec(kind="turn", turn=turns, malformed=raw[:2000])
                msgs.append({"role": "user", "content": "That was not valid JSON. Emit one action object."})
                continue

            msgs.append({"role": "assistant", "content": raw})
            if verbose:
                print(f"    [{turns}] {a.get('tool')} {a.get('path') or a.get('query') or ''}"[:110])

            # `finish` goes through dispatch like everything else, so that the
            # no-edit check and the stall rule both apply to it.
            obs = s.dispatch(a)
            rec(kind="turn", turn=turns, action=a, obs=obs)
            if obs is None:
                break
            if obs.endswith("ABORT"):
                if verbose:
                    print("          -> aborting: stalled three times on the same action")
                break
            if verbose:
                print(f"          -> {obs.splitlines()[0][:110]}")
            msgs.append({"role": "user", "content": obs[:OBS_CAP]})
        finally:
            # runs on break and continue too, so every turn is timed exactly once
            turn_s.append(round(time.time() - t_turn, 1))

    wall = time.time() - t0
    ok, diag, _ = s.run_check()
    # An oracle that passes on an untouched checkout measures nothing, so every
    # task has one and `--selftest` asserts each fails on a pristine tree.
    # Falling back to `final_check_ok` would do exactly the wrong thing: a run
    # where the model did nothing at all would score as a pass.
    oracle_ok = False
    if task.get("oracle_cmd"):
        p = subprocess.run(task["oracle_cmd"], cwd=repo, env=s.env,
                           capture_output=True, text=True, shell=True)
        oracle_ok = p.returncode == 0

    return {
        "task": task["name"], "turns": turns, "wall_s": round(wall, 1),
        "turn_s": turn_s, "median_turn_s": sorted(turn_s)[len(turn_s) // 2] if turn_s else None,
        "wfa": (wfa_ok, wfa_total),
        "edit_actions": s.edit_actions, "edits_applied": s.edits_applied,
        "edits_ok_first_apply": s.edits_first_apply_ok, "reverts": s.reverts,
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

# §12's smoke suite says "each with a `cargo test` oracle", so each task gets one
# that checks the change actually happened *and* the crate still tests green.
# Behavioural checks where a grep is honest, a compiled assertion where it is not.
TESTS = "cargo test -p dipper-index --quiet && cargo check --workspace --all-targets"

DISPLAY_ORACLE = r"""
mkdir -p crates/dipper-index/tests
cat > crates/dipper-index/tests/hg_oracle.rs <<'EOF'
use dipper_index::{Hit, Record};

#[test]
fn hit_displays_identifier_and_score() {
    let h = Hit {
        record: Record { identifier: "nasa-apollo".into(), ..Default::default() },
        score: 12.5,
    };
    assert_eq!(format!("{h}"), "nasa-apollo (12.5)");
}
EOF
cargo test -p dipper-index --test hg_oracle --quiet
rc=$?
rm -f crates/dipper-index/tests/hg_oracle.rs
exit $rc
"""

TASKS = [
    {"name": "rename", "preload": [IDX],
     # The prompt used to say "every caller in that file". It lied: `dipper-cli`
     # calls `Catalogue::count` too, so following the prompt exactly leaves the
     # workspace red. Under the old revert-on-red gate that made the task
     # unwinnable. A longer version of this prompt, adding "`cargo check` will
     # tell you where they are", drew an argument-less `edit` in 3 of 5 runs
     # against 0 of 4 for the shorter wording; unexplained, and left out rather
     # than carried as a confound.
     "prompt": "In crates/dipper-index/src/lib.rs, rename the public method `count` on "
               "`Catalogue` to `matches`. Update every caller in the workspace too.",
     "oracle_cmd": f"grep -q 'pub fn matches(' {IDX} && ! grep -q 'pub fn count(' {IDX} && {TESTS}"},
    {"name": "add_field", "preload": [IDX],
     # §12's seed set says "add a struct field and fix all constructors", and two
     # struct literals in this file do break, so the prompt has to say so.
     # HG_ADDFIELD=short drops the second clause. Under the old revert-on-red
     # gate the model could never see the breakage, so the prompt had to describe
     # it; under the accumulate gate the compiler says it directly. Whether the
     # longer wording still helps, or hurts by drawing argument-less edits, is
     # the A/B this switch exists for.
     "prompt": "In crates/dipper-index/src/lib.rs, add a new public field "
               "`pub language: Option<String>,` to the `Record` struct, after the `creator` "
               "field" + ("." if os.environ.get("HG_ADDFIELD") == "short" else
                          ", and fix every `Record { .. }` literal in the file that stops "
                          "compiling."),
     "oracle_cmd": f"grep -A1 'pub creator:' {IDX} | grep -qF 'pub language: Option<String>,' && {TESTS}"},
    {"name": "fix_test", "preload": [IDX],
     "prompt": "The test suite in crates/dipper-index is failing. Find the bug in "
               "`Catalogue::is_empty` in crates/dipper-index/src/lib.rs and fix it.",
     "setup": plant_failing_test,
     "oracle_cmd": f"grep -qF 'Ok(self.len()? == 0)' {IDX} && {TESTS}"},
    {"name": "display_impl", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, add an `impl std::fmt::Display for Hit` "
               "that writes the record's identifier followed by the score in parentheses, "
               "for example `nasa-apollo (12.5)`.",
     "oracle_cmd": DISPLAY_ORACLE},
    {"name": "doc_comment", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, the method `Catalogue::is_empty` has no "
               "doc comment. Add a one-line `///` doc comment above it describing what it returns.",
     "oracle_cmd": f"grep -B1 'pub fn is_empty(' {IDX} | grep -q '///' && {TESTS}"},
]


def selftest(repo, target_dir):
    """Every oracle must FAIL on a pristine checkout. An oracle that passes when
    nothing has happened is the failure mode §12.1 exists to prevent."""
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, capture_output=True)
    env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir))
    bad = 0
    for t in TASKS:
        if t.get("setup"):
            t["setup"](repo)
        p = subprocess.run(t["oracle_cmd"], cwd=repo, env=env, shell=True,
                           capture_output=True, text=True)
        # fix_test plants its bug first, so "pristine" for it means the bug present
        ok = p.returncode != 0
        print(f"  {t['name']:<13} pristine rc={p.returncode} -> {'fails, good' if ok else 'PASSES, USELESS'}")
        bad += not ok
        subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "clean", "-fdq"], cwd=repo, capture_output=True)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--target-dir", default="/tmp/hg-m0-target")
    ap.add_argument("--phases", type=int, default=1, choices=[1, 2])
    ap.add_argument("--task", default=None)
    ap.add_argument("--brief", default=str(HERE / "dipper-brief.md"))
    ap.add_argument("--no-brief", action="store_true",
                    help="run without the project brief, for the index-value A/B")
    ap.add_argument("--out", default=None, help="write the result JSON here")
    ap.add_argument("--transcript", default=None, help="append a per-turn JSONL event log here")
    ap.add_argument("--trials", type=int, default=1,
                    help="repeat the whole suite N times; §12.1 rule 4 wants three")
    ap.add_argument("--selftest", action="store_true",
                    help="assert every oracle fails on a pristine checkout, then exit")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    if a.selftest:
        print(f"oracle self-test against {repo}")
        bad = selftest(repo, Path(a.target_dir))
        print("all oracles fail on a pristine tree" if not bad else f"{bad} oracle(s) useless")
        return 1 if bad else 0

    brief = "" if a.no_brief else (Path(a.brief).read_text() if Path(a.brief).exists() else "")
    tasks = [t for t in TASKS if a.task in (None, t["name"])]

    print(f"model={MODEL} host={HOST} phases={a.phases} brief={not a.no_brief} repo={repo}")
    print(f"warming cargo cache ...", flush=True)
    # Warm exactly what the gate runs, or the first turn of the first task pays
    # the cold build and lands in the median the second gate is measured against.
    subprocess.run("cargo check --workspace --all-targets", cwd=repo, shell=True,
                   capture_output=True, env=dict(os.environ, CARGO_TARGET_DIR=a.target_dir))
    # §12.1 rule 1: warm the model too. An unwarmed first turn folds model load
    # into the turn we are about to gate on.
    print(f"warming model ...", flush=True)
    call([{"role": "user", "content": "ok"}], num_predict=1)

    results = []
    for trial in range(1, a.trials + 1):
        for t in tasks:
            print(f"\n=== {t['name']}{f' (trial {trial})' if a.trials > 1 else ''} ===", flush=True)
            r = run_task(t, repo, Path(a.target_dir), a.phases, brief,
                         transcript=a.transcript)
            r["trial"] = trial
            results.append(r)
            print("   ", json.dumps(r), flush=True)

    ep = sum(r["edit_actions"] for r in results)
    ea = sum(r["edits_applied"] for r in results)
    eo = sum(r["edits_ok_first_apply"] for r in results)
    wo = sum(r["wfa"][0] for r in results)
    wt = sum(r["wfa"][1] for r in results)
    all_turns = sorted(s for r in results for s in r["turn_s"])
    med_turn = all_turns[len(all_turns) // 2] if all_turns else 0
    summary = {
        "model": MODEL, "host": HOST, "phases": a.phases, "brief": not a.no_brief,
        "trials": a.trials,
        "repo": str(repo), "repo_rev": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
            text=True).stdout.strip(),
        "wfa": [wo, wt],
        "facp_applied": [eo, ea], "facp_proposed": [eo, ep],
        "oracle": [sum(1 for r in results if r["oracle_ok"]), len(results)],
        "median_turn_s": med_turn,
        "total_wall_s": round(sum(r["wall_s"] for r in results), 1),
        "results": results,
    }
    print("\n" + "=" * 66)
    print(f"WFA    {wo}/{wt} = {100*wo/max(wt,1):.0f}%")
    print(f"FACP   {eo}/{ea} = {100*eo/max(ea,1):.0f}%  of edits that applied   (M0 gate G2: 60%)")
    print(f"       {eo}/{ep} = {100*eo/max(ep,1):.0f}%  of edits proposed")
    print(f"oracle {summary['oracle'][0]}/{len(results)} tasks actually done")
    if a.trials > 1:
        # §12.1 rule 4: report every trial, not the flattering one
        for trial in range(1, a.trials + 1):
            rs = [r for r in results if r["trial"] == trial]
            done = [r["task"] for r in rs if r["oracle_ok"]]
            print(f"       trial {trial}: {len(done)}/{len(rs)}  {' '.join(done) or '-'}")
    print(f"median turn    {med_turn}s over {len(all_turns)} turns   (M0 gate: under 120s)")
    print(f"median turns/task {sorted(r['turns'] for r in results)[len(results)//2]}")
    print(f"total wall     {summary['total_wall_s']:.0f}s")
    print("=" * 66)
    if a.out:
        Path(a.out).write_text(json.dumps(summary, indent=1))
        print(f"written to {a.out}")
    else:
        print(json.dumps(results, indent=1))


if __name__ == "__main__":
    sys.exit(main())
