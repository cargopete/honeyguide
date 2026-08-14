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
import shutil
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

MAX_TURNS = 12          # RFC-0003 §6: overridable, because one M0 `rename` trial
                        # hit this cap with the error count still falling. That is a
                        # stopwatch, not a model failure, and it needs measuring.
MAX_EDIT_LINES = 200
OBS_CAP = 4000          # characters; observations are truncated hard (§8.5)
GUTTER = re.compile(r"^\s*\d+\|", re.M)
MAX_NO_PROGRESS = 3     # red gates without reducing the error count, then roll back
RESAMPLE_TEMPS = (0.3, 0.7, 1.0)    # RFC-0003 §4: the stall re-roll ladder
ERRLINE = re.compile(r"^error(\[[A-Z]\d+\])?: (.*)$", re.M)


def wilson(k, n, z=1.96):
    """95% Wilson interval for k successes in n. Reported beside every rate
    because RFC-0003 §0: five configurations of this suite scored 4/15 to 9/15
    and every one of them sat inside a single interval. A bare rate from a small
    sample is not a measurement, it is an anecdote with a denominator."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k, n):
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / max(n, 1):.0f}%  [95% CI {100 * lo:.0f}-{100 * hi:.0f}%]"


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


def call(messages, schema=None, num_predict=600, temperature=0.3):
    payload = {
        "model": MODEL, "messages": messages, "stream": False, "think": False,
        "keep_alive": "30m",
        "options": {"temperature": temperature, "top_p": 0.95, "top_k": 20, "min_p": 0.05,
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


# --------------------------------------------------------------- formatting

def _edition(repo: Path) -> str:
    """Whatever the workspace declares, because rustfmt parses differently per
    edition and guessing wrong turns a formatter into a syntax error."""
    import tomllib
    try:
        cfg = tomllib.loads((repo / "Cargo.toml").read_text())
    except Exception:
        return "2021"
    return str(cfg.get("workspace", {}).get("package", {}).get("edition")
               or cfg.get("package", {}).get("edition") or "2021")


def fmt_clean(path: Path, edition: str) -> bool:
    p = subprocess.run(["rustfmt", "--edition", edition, "--check", str(path)],
                       capture_output=True, text=True)
    return p.returncode == 0


def fmt_file(path: Path, edition: str) -> bool:
    """Format one file. Returns whether it changed.

    The gate checks that code compiles, not that it is presentable, and the first
    live free-form run produced a correct doc comment indented eight spaces
    instead of four. It compiled, so the gate passed it, and it would have failed
    the target repository's own `cargo fmt --check` CI.

    Formatting is deterministic and costs no model tokens, so the harness does it
    rather than asking. It only runs on a file that was *already* canonically
    formatted before the edit: reformatting someone's non-canonical file would
    bury a one-line change in a thousand-line diff, and that is not ours to do.
    """
    before = path.read_text()
    subprocess.run(["rustfmt", "--edition", edition, str(path)],
                   capture_output=True, text=True)
    return path.read_text() != before


# ------------------------------------------- mechanical propagation (RFC-0003 §3)

SCIP_REFS_BIN = REPO_ROOT / "target" / "debug" / "hg-scip-refs"


def _def_pos(text, search, old):
    """Zero-based line and column of the definition of `old` within the matched
    `search`, in the pre-edit file. SCIP is queried by position, not by name,
    which is the entire point (§3.2a)."""
    base = text.index(search)
    m = re.search(DEFINES.format(re.escape(old)), search)
    if not m:
        return None
    off = base + m.end() - len(old)
    return text.count("\n", 0, off), off - (text.rfind("\n", 0, off) + 1)


def propagate_via_scip(repo: Path, scip: Path, rel: str, line: int, col: int,
                       old: str, new: str, snapshot=None):
    """Rename every reference to the symbol *defined at this position*.

    The reference set comes from `hg-scip-refs`, which resolves it through
    rust-analyzer rather than by matching text. Each site is still verified
    against what is on disk before it is touched, because §5.2 is explicit that
    the index is a map and never ground truth for file contents: an index built
    before earlier edits in this session may point at a line that has moved.
    A site that does not verify is skipped and named, never guessed at.
    """
    if not SCIP_REFS_BIN.is_file() or not Path(scip).is_file():
        return [], [], "no SCIP index"
    p = subprocess.run([str(SCIP_REFS_BIN), str(scip), rel, str(line), str(col)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return [], [], f"hg-scip-refs failed: {p.stderr.strip()[:120]}"
    try:
        data = json.loads(p.stdout)
    except Exception:
        return [], [], "hg-scip-refs emitted nothing parseable"
    if not data.get("symbol"):
        return [], [], "no symbol is defined at that position"

    edits = collections.defaultdict(list)
    for r in data["refs"]:
        edits[r["path"]].append((r["line"], r["col_start"], r["col_end"]))

    changed, skipped = [], []
    for rel_path, sites in edits.items():
        f = (repo / rel_path).resolve()
        if not f.is_file():
            skipped.append(f"{rel_path} (missing)")
            continue
        orig = f.read_text(errors="replace")
        lines = orig.splitlines(keepends=True)
        n = 0
        # bottom-up, so earlier edits do not move later columns on the same line
        for ln, cs, ce in sorted(sites, reverse=True):
            if ln >= len(lines):
                skipped.append(f"{rel_path}:{ln + 1} (past end of file)")
                continue
            body = lines[ln]
            if body[cs:ce] != old:
                skipped.append(f"{rel_path}:{ln + 1} (text has moved)")
                continue
            lines[ln] = body[:cs] + new + body[ce:]
            n += 1
        if n:
            if snapshot:
                snapshot(f, orig)
            f.write_text("".join(lines))
            changed.append((rel_path, n))
    return changed, skipped, None


TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFINES = r"\b(fn|struct|enum|trait|type|mod|const|static|union)\s+{}\b"


DEF_SITE = re.compile(r"\b(fn|struct|enum|trait|type|mod|const|static|union)\s+"
                      r"([A-Za-z_][A-Za-z0-9_]*)")


def detect_rename_by_definition(search, replace):
    """Did this edit rename exactly one *definition*? Returns (old, new) or None.

    This replaces a whole-text comparison that was correct and useless. Requiring
    `search` and `replace` to be identical but for one token means the edit must
    be minimal, and models do not write minimal edits. Measured: asked to rename
    `Catalogue::count`, the driver emitted

        search:  "    /// How many documents match a query.\\n    pub fn count(..."
        replace: "pub fn matches(..."

    which renames the method, drops the doc comment and drops the indentation.
    A pure-rename test says no, propagation never fires, and the whole feature
    sat inert through a fifteen-task suite doing nothing at all.

    Comparing definitions instead ignores everything that is not a declaration.
    Still conservative: exactly one definition name may disappear and exactly one
    appear, and their keywords must match, or this is not a rename.
    """
    a = DEF_SITE.findall(search)
    b = DEF_SITE.findall(replace)
    gone = [d for d in a if d not in b]
    came = [d for d in b if d not in a]
    if len(gone) != 1 or len(came) != 1:
        return None
    (kind_a, old), (kind_b, new) = gone[0], came[0]
    if kind_a != kind_b or old == new:
        return None
    return old, new


def detect_rename(search, replace):
    """Is this edit a pure rename of one identifier? Returns (old, new) or None.

    Kept for the strictest case and for its tests; `detect_rename_by_definition`
    is what the gate uses, because this one almost never matches real output.
    """
    a, b = TOKEN.findall(search), TOKEN.findall(replace)
    if len(a) != len(b) or TOKEN.sub("\0", search) != TOKEN.sub("\0", replace):
        return None                                  # skeleton differs: not a rename
    # Distinct substitutions, not differing positions: renaming a function and
    # its own recursive call inside one block is still a single rename.
    diff = {(x, y) for x, y in zip(a, b) if x != y}
    if len(diff) != 1:
        return None
    old, new = diff.pop()
    # every occurrence of the old token must have become the new one, or this is
    # a rename of one use rather than of the symbol
    if any(x == old and y != new for x, y in zip(a, b)):
        return None
    return (old, new) if old and new and old != new else None


def _mask(line):
    """Blank out string literals and trailing comments, so propagation never
    rewrites prose or a user-visible message. Crude, and deliberately so: the
    cost of a miss is a skipped site the model can still fix by hand, and the
    cost of a false hit is silently changing a string the compiler cannot check."""
    out = list(line)
    i, in_str = 0, False
    while i < len(line):
        c = line[i]
        if in_str:
            out[i] = " "
            if c == "\\":
                if i + 1 < len(line):
                    out[i + 1] = " "
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out[i] = " "
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            for j in range(i, len(line)):
                out[j] = " "
            break
        i += 1
    return "".join(out)


def propagate_rename(repo: Path, old: str, new: str, skip: Path, snapshot=None):
    """Apply `old` -> `new` at every whole-token reference outside `skip`.

    **This is unsafe on a lexical reference set and is off by default.** Tried on
    dipper, renaming `Catalogue::count` rewrote 84 sites across 15 files:
    `Iterator::count()`, struct fields named `count`, local bindings named
    `count`. None of them referenced the renamed method. A whole-token grep
    cannot tell a method on one type from an identical name on another, so this
    is fuzzy application by another name, and RFC-0001 §7 forbids it for exactly
    this outcome.

    Kept because the mechanism around it is right and only the reference set is
    wrong: `scip.sqlite` answers "every reference to *this* symbol" exactly, and
    at that point this function becomes correct without changing. Until then it
    requires `--propagate` and should only be pointed at a repository you are
    willing to lose.
    """
    if shutil.which("rg"):
        cmd = ["rg", "-l", "-w", "-F", old, "--glob", "*.rs", "."]
    else:
        cmd = ["grep", "-rlwF", "--include=*.rs", old, "."]
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)

    changed, skipped = [], []
    for rel in (p.stdout or "").split():
        rel = rel.lstrip("./")
        f = (repo / rel).resolve()
        if not f.is_file() or f == skip.resolve():
            continue
        orig = f.read_text(errors="replace")
        lines, n = orig.splitlines(keepends=True), 0
        for i, line in enumerate(lines):
            masked = _mask(line)
            hits = [m for m in TOKEN.finditer(masked) if m.group(0) == old]
            if not hits:
                continue
            buf = list(line)
            for m in reversed(hits):
                buf[m.start():m.end()] = new
            lines[i] = "".join(buf)
            n += len(hits)
        if n:
            if snapshot:
                snapshot(f, orig)      # so §6.2's rollback can restore these too
            f.write_text("".join(lines))
            changed.append((rel, n))
        else:
            skipped.append(rel)                      # matched only in strings/comments
    return changed, skipped


# ---------------------------------------------------------------------- tools

class Session:
    """One task attempt against one overlay."""

    def __init__(self, repo: Path, target_dir: Path, check_cmd, propagate=False,
                 scip=None):
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
        self.propagate = propagate      # RFC-0003 §3
        self.propagated = 0
        self.edition = _edition(repo)
        self.formatted = 0
        self.scip = scip

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
        was_fmt_clean = self.edition and fmt_clean(p, self.edition)
        p.write_text(text.replace(search, replace, 1))

        # RFC-0003 §3. If this edit renamed a definition, the remaining work is
        # the identical substitution at sites the harness already knows. Neither
        # model completed such a task in twelve attempts; the harness does it in
        # milliseconds. Propagation happens before the gate so the whole batch is
        # judged as one change.
        note = ""
        if was_fmt_clean and fmt_file(p, self.edition):
            self.formatted += 1
            note += " The harness reformatted the file with rustfmt."
        if self.propagate and self.scip:
            ren = detect_rename_by_definition(search, replace)
            pos = _def_pos(text, search, ren[0]) if ren else None
            if pos:
                rel = str(p.relative_to(self.repo.resolve()))
                changed, skipped, why = propagate_via_scip(
                    self.repo, self.scip, rel, pos[0], pos[1], ren[0], ren[1],
                    snapshot=lambda q, t: self.green.setdefault(str(q), t))
                if changed:
                    self.propagated += sum(n for _, n in changed)
                    where = ", ".join(f"{r} ({n})" for r, n in changed)
                    note = (f"\nThe harness also renamed `{ren[0]}` to `{ren[1]}` at "
                            f"{sum(n for _, n in changed)} reference site(s): {where}.")
                    if skipped:
                        note += " Skipped: " + ", ".join(skipped) + "."
                elif why:
                    note = f"\n(no references propagated: {why})"
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
            return (f"edit applied to {path} and `cargo check` passed in {secs:.1f}s.{note} "
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
        return (f"edit applied to {path}, but `cargo check` now FAILS with {trend}.{note} The "
                "edit was KEPT. Fix these errors with your next edit; if they are the expected "
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

    def would_stall(self, a):
        """Whether this action would trip the stall rule, without recording it.

        `read` is excluded: a repeated read is *satisfied* by paging forward
        (§6.3), so it is not a wasted turn and there is nothing to re-roll.
        """
        tool = a.get("tool")
        if tool not in self.SIG_FIELDS or tool == "read":
            return False
        sig = (tool,) + tuple(str(a.get(f) or "") for f in self.SIG_FIELDS[tool])
        return sig in self.recent

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

def run_task(task, repo, target_dir, phases, brief, verbose=True, transcript=None,
             max_turns=MAX_TURNS, reset=True, propagate=False, resample=False,
             scip=None):
    # `reset` is False for free-form runs. The suite resets because its fixtures
    # depend on a known starting state; doing that to a repository someone is
    # actually working in would delete their uncommitted work.
    if reset:
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
                     task.get("check_cmd", "cargo check --workspace --all-targets"),
                     propagate=propagate, scip=scip)
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
    if task.get("preload_text"):
        # free-form mode assembles its own context (§5.3) and marks the paths it
        # supplied as read, so the read-before-edit precondition is satisfied by
        # provision rather than costing a turn.
        preload += task["preload_text"]
        for rel in task.get("preload_paths", []):
            s0.read_paths.add(str((repo / rel).resolve()))

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

    wfa_ok = wfa_total = resamples = 0
    t0 = time.time()
    turns = 0
    turn_s = []          # §13's second gate is a *median turn*, so time each one
    for turns in range(1, max_turns + 1):
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

            # RFC-0003 §4. A refusal asks the model again under conditions almost
            # identical to the ones that just produced the bad action, at a
            # temperature chosen to suppress variety. Re-roll first: the prefix is
            # unchanged so this is cached prefill plus ~80 output tokens, about
            # three seconds against a 120s budget.
            a = None
            for temp in (RESAMPLE_TEMPS if resample else RESAMPLE_TEMPS[:1]):
                wfa_total += 1
                r = call(msgs, schema=SCHEMA, num_predict=900, temperature=temp)
                raw = (r.get("message") or {}).get("content") or ""
                try:
                    cand = json.loads(raw)
                    wfa_ok += 1
                except Exception:
                    rec(kind="turn", turn=turns, malformed=raw[:2000])
                    a = None
                    break
                a = cand
                if not s.would_stall(cand):
                    break
                resamples += 1
                if verbose:
                    print(f"    [{turns}] resampling, that would have stalled "
                          f"(temp {temp} -> next)")
            if a is None:
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
        "formatted": s.formatted, "resamples": resamples,
        "propagated_sites": s.propagated,
        "final_check_ok": ok, "oracle_ok": oracle_ok,
        "refusals": s.refusals,
    }


# --------------------------------------------------- free-form mode (RFC-0003 §7)
#
# Everything above this line serves the fixture suite. This section is what makes
# the spike drivable against a real repository with a real request, which is the
# only way to find out whether the loop is usable rather than merely measurable.
#
# It stands in for two things the Rust crates will own: §5.1's degraded index
# (G4, no model involved) and §5.3's retrieval. Both are deliberately structural
# and lexical. Nothing here asks a model anything.

def degraded_brief(repo: Path) -> str:
    """§5.1 pass 3. With no semantic pass available, build the project brief from
    `Cargo.toml` metadata and the crate list. This is what `hg index --no-llm`
    produces, and M0 measured no reliable difference between a hand-written brief
    and none at all, so it is not obviously worse than the expensive version."""
    import tomllib
    root = repo / "Cargo.toml"
    if not root.is_file():
        return ""
    try:
        cfg = tomllib.loads(root.read_text())
    except Exception:
        return ""

    members = cfg.get("workspace", {}).get("members", [])
    rows = []
    for pat in members:
        for d in sorted(repo.glob(pat)):
            man = d / "Cargo.toml"
            if not man.is_file():
                continue
            try:
                m = tomllib.loads(man.read_text()).get("package", {})
            except Exception:
                continue
            name = m.get("name", d.name)
            desc = m.get("description", "")
            src = d / "src"
            n = sum(len(f.read_text(errors="replace").splitlines())
                    for f in src.rglob("*.rs")) if src.is_dir() else 0
            rows.append(f"| `{name}` | {n} lines | {desc} |")

    pkg = cfg.get("package", {}) or cfg.get("workspace", {}).get("package", {})
    out = [f"# Project brief: {repo.name}", ""]
    if pkg.get("description"):
        out += [pkg["description"], ""]
    out += ["Generated structurally from Cargo.toml. No semantic pass has run,",
            "so this describes the shape of the workspace and nothing about intent.",
            "", "Build and test:", "", "```", "cargo check --workspace --all-targets",
            "cargo test -p <crate>", "```", ""]
    if rows:
        out += ["| Crate | Size | Description |", "|---|---|---|"] + rows + [""]
    return "\n".join(out)


# Identifiers worth searching for: anything backticked, any CamelCase word, and
# any snake_case word of four characters or more. The length floor keeps "the",
# "add" and "fix" out of the search, which otherwise match the entire repository.
IDENT = re.compile(r"`([^`]+)`|\b([A-Z][A-Za-z0-9_]{2,}|[a-z_][a-z0-9_]{2,})\b")
STOPWORDS = {"the", "and", "that", "this", "with", "from", "into", "make", "have",
             "then", "when", "where", "which", "should", "would", "there", "their",
             "every", "also", "does", "your", "file", "line", "code", "test",
             "tests", "function", "method", "struct", "field", "public", "return",
             "rename", "update", "change", "caller", "callers", "crates", "src"}


def _idents(text):
    out = []
    for m in IDENT.finditer(text):
        tok = (m.group(1) or m.group(2) or "").strip()
        tok = re.sub(r"^[^A-Za-z_]+|[^A-Za-z0-9_]+$", "", tok)
        if tok and tok.lower() not in STOPWORDS and not tok.endswith(".rs"):
            out.append(tok)
    # paths mentioned literally are the strongest signal of all
    out += re.findall(r"[\w./-]+\.rs", text)
    return list(dict.fromkeys(out))


def retrieve(repo: Path, task_text: str, max_files=2, max_lines=500, verbose=True):
    """§5.3 retrieval, structural only. Score files by how many distinct
    identifiers from the request they contain, take the best few, and supply
    them. Big files are supplied as the regions around the matches rather than
    whole, because §8.5 pays for every context token on every subsequent turn."""
    idents = _idents(task_text)
    if not idents:
        return "", [], []

    def rg(pattern, fixed):
        base = ["rg", "-n", "--no-heading", "--glob", "*.rs"] if shutil.which("rg") \
            else ["grep", "-rnE", "--include=*.rs"]
        if shutil.which("rg"):
            cmd = base + (["-w", "-F"] if fixed else []) + [pattern, "."]
        else:
            cmd = (["grep", "-rnwF", "--include=*.rs", pattern, "."] if fixed
                   else ["grep", "-rnE", "--include=*.rs", pattern, "."])
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        out = collections.defaultdict(set)
        for line in (p.stdout or "").splitlines()[:800]:
            parts = line.split(":", 2)
            rel = parts[0].lstrip("./")
            if len(parts) < 3 or not rel.endswith(".rs"):
                continue
            try:
                out[rel].add(int(parts[1]))
            except ValueError:
                pass
        return out

    # The discriminating question is not "does this word appear" but "is this a
    # symbol". `Add` at the start of a sentence is not a Rust type, and scoring it
    # as one is how a request about `Record` in `dipper-index` returned a
    # BitTorrent discovery module. So each candidate is first tested for a
    # *definition* anywhere in the tree. This is the lexical stand-in for §5.3's
    # "match against `symbols.sig`", and `scip.sqlite` replaces it at M1.
    DEF = r"\b(fn|struct|enum|trait|type|mod|const|static|impl|macro_rules!)\s+{}\b"
    refs, defs, symbols = {}, {}, []
    for tok in idents[:12]:
        if tok.endswith(".rs"):
            hits = rg(tok, True)
            if hits:
                refs[tok], defs[tok], symbols = hits, hits, symbols + [tok]
            continue
        d = rg(DEF.format(re.escape(tok)), False)
        if not d:
            continue                         # not a symbol in this repo: English
        symbols.append(tok)
        defs[tok] = d
        refs[tok] = rg(tok, True)

    if not symbols:
        return "", [], idents

    # Ubiquity is relative to the repository, not to our own matches. Computed the
    # other way, `picker` in 6 files looked ubiquitous because our matches only
    # touched 8, and the request about the piece picker retrieved nothing at all.
    repo_files = max(1, sum(1 for _ in repo.rglob("*.rs")))
    score = collections.defaultdict(float)
    lines_of = collections.defaultdict(set)

    # People name modules and crates after what they do, so a request mentioning
    # "the magnet link parser" is pointing at `magnet.rs` whether or not `magnet`
    # happens to be a symbol elsewhere. Without this, a coincidental definition
    # (`fn parser` in another crate) outranked the module actually named.
    for tok in idents[:12]:
        low = tok.lower()
        for f in repo.rglob("*.rs"):
            rel = str(f.relative_to(repo))
            if f.stem.lower() == low:
                score[rel] += 6.0
            elif low in rel.lower().split("/")[:-1]:
                score[rel] += 0.5            # somewhere under a directory of that name
            elif any(low in part.lower().split("-") for part in rel.split("/")[:2]):
                score[rel] += 0.4            # crate name, e.g. "cli" in dipper-cli
    for tok in symbols:
        # The definition bonus is unconditional. Even a common symbol's *defining*
        # file is the right place to look, and gating it behind the ubiquity test
        # threw away the best signal available.
        for rel, ln in defs[tok].items():
            score[rel] += 5.0
            lines_of[rel] |= ln
        where = refs[tok]
        if len(where) > max(6, repo_files * 0.25):
            continue                         # genuinely everywhere: discriminates nothing
        weight = 1.0 / len(where)
        for rel, ln in where.items():
            score[rel] += weight
            lines_of[rel] |= ln

    if not score:
        return "", [], idents
    ranked = sorted(score, key=lambda r: (-score[r], len(r)))[:max_files]
    if verbose:
        print(f"retrieval: symbols {symbols} -> "
              + ", ".join(f"{r} {score[r]:.1f}" for r in ranked))
    body, used, budget = "", [], max_lines
    for rel in ranked:
        f = repo / rel
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        used.append(rel)
        if len(lines) <= budget:
            body += (f"\n\n{rel} ({len(lines)} lines, complete):\n"
                     + "\n".join(f"{i:>4}| {l}" for i, l in enumerate(lines, 1)))
            budget -= len(lines)
            continue
        # too big: give the neighbourhoods of the matches, merged
        spans, ctx = [], 40
        for n in sorted(lines_of[rel]):
            lo, hi = max(1, n - ctx), min(len(lines), n + ctx)
            if spans and lo <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
            else:
                spans.append((lo, hi))
        body += f"\n\n{rel} ({len(lines)} lines, showing the parts that matched):"
        for lo, hi in spans:
            if budget <= 0:
                body += "\n  ... truncated, use `read` for the rest"
                break
            hi = min(hi, lo + budget - 1)
            budget -= hi - lo + 1
            body += ("\n" + "\n".join(f"{i:>4}| {lines[i-1]}" for i in range(lo, hi + 1))
                     + "\n  ...")
    if verbose:
        print(f"retrieval: {len(idents)} identifiers -> {', '.join(used) or 'nothing'}"
              f"  ({max_lines - budget} lines supplied)")
    return body, used, idents


def make_overlay(repo: Path, dest: Path):
    """Q2, closed: a whole-tree copy-on-write clone where the filesystem supports
    it, a plain copy where it does not. Never a hardlink forest: measured, it
    writes through to the working tree."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for flags in ("-Rc", "-R"):
        p = subprocess.run(f"cp {flags} {repo}/. {dest}", shell=True,
                           capture_output=True, text=True)
        if p.returncode == 0:
            return flags
    raise RuntimeError(f"could not build an overlay at {dest}")


def free_form(a, repo: Path):
    """`--prompt "..."`: one ad-hoc task against a real repository, worked in an
    overlay, handed back as a patch. The working tree is never touched."""
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    overlay = work / "overlay"
    flags = make_overlay(repo, overlay)
    # Commit whatever state the working tree was in, inside the throwaway
    # overlay only, so the final diff shows what *this run* did and not the
    # user's own uncommitted work. Without this a run reported edits it had not
    # made, which is the same class of error as an oracle that passes on an
    # untouched tree.
    for cmd in (["git", "add", "-A"],
                ["git", "-c", "user.email=hg@localhost", "-c", "user.name=honeyguide",
                 "commit", "-q", "--allow-empty", "-m", "hg overlay baseline"]):
        subprocess.run(cmd, cwd=overlay, capture_output=True)
    print(f"overlay: cp {flags} -> {overlay}")

    brief = "" if a.no_brief else (Path(a.brief).read_text()
                                   if a.brief and Path(a.brief).is_file()
                                   else degraded_brief(overlay))
    preload, used, idents = retrieve(overlay, a.prompt)
    if not used:
        print("retrieval found nothing; the model will have to `search` for itself")

    print(f"warming: {a.max_turns} turn cap, gate `cargo check --workspace --all-targets`",
          flush=True)
    subprocess.run("cargo check --workspace --all-targets", cwd=overlay, shell=True,
                   capture_output=True,
                   env=dict(os.environ, CARGO_TARGET_DIR=a.target_dir))
    call([{"role": "user", "content": "ok"}], num_predict=1)

    task = {"name": "adhoc", "prompt": a.prompt,
            "preload_text": preload, "preload_paths": used}
    r = run_task(task, overlay, Path(a.target_dir), a.phases, brief,
                 transcript=a.transcript, max_turns=a.max_turns, reset=False,
                 propagate=a.propagate, resample=a.resample, scip=a.scip)

    diff = subprocess.run(["git", "diff", "HEAD"], cwd=overlay, capture_output=True,
                          text=True).stdout
    print("\n" + "=" * 66)
    print(f"turns {r['turns']}   wall {r['wall_s']}s   gate {'GREEN' if r['final_check_ok'] else 'RED'}"
          f"   edits {r['edit_actions']}/{r['edits_applied']}/{r['edits_ok_first_apply']}")
    if r["refusals"]:
        print("refusals:", ", ".join(r["refusals"]))
    print("=" * 66)
    if not diff.strip():
        print("no changes were made.")
        return 1
    patch = work / "changes.patch"
    patch.write_text(diff)
    print(diff)
    print("=" * 66)
    if not r["final_check_ok"]:
        print("the gate is RED, so this patch does not compile. Not offered for apply.")
        print(f"patch written anyway for inspection: {patch}")
        return 1
    print(f"patch written to {patch}")
    print(f"apply with:  git -C {repo} apply {patch}")
    return 0


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
    # --- added when §0 showed five tasks could not measure anything ---------
    # §12's seed set always said "roughly ten". Two of these are chosen because
    # they punish a lexical approach specifically: `dipper-web` declares its own
    # `Hit`, and `Fields` its own `downloads`, so a token rename corrupts them
    # while a SCIP rename must leave them alone.
    {"name": "rename_type", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, rename the public struct `Hit` to "
               "`SearchHit`. Update every use of it in the workspace too.",
     "oracle_cmd": (f"grep -q 'pub struct SearchHit' {IDX} && ! grep -q 'pub struct Hit' {IDX} "
                    "&& grep -q 'SearchHit' crates/dipper-cli/src/main.rs "
                    # dipper-web has an unrelated `Hit` of its own; touching it is a failure
                    "&& grep -q 'pub struct Hit' crates/dipper-web/src/search.rs "
                    f"&& {TESTS}")},
    {"name": "rename_field", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, rename the public field `downloads` on "
               "the `Record` struct to `download_count`. Update every use of it.",
     "oracle_cmd": (f"grep -q 'pub download_count: u64' {IDX} "
                    # the private `Fields` struct has its own `downloads`, on the same
                    # line as a use of Record's in one place. It must survive.
                    f"&& grep -q 'downloads: Field' {IDX} "
                    f"&& grep -q 'f.downloads => record.download_count' {IDX} "
                    f"&& {TESTS}")},
    {"name": "add_error_variant", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, add a new variant `Locked` to the `Error` "
               "enum, carrying no data, with the thiserror message \"index is locked\".",
     "oracle_cmd": (f"grep -q 'index is locked' {IDX} && grep -qE '^\\s*Locked,' {IDX} "
                    f"&& {TESTS}")},
    {"name": "derive_hash", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, make the `Record` struct usable as a "
               "hash-map key by adding `Hash` to its derive list.",
     "oracle_cmd": r"""
mkdir -p crates/dipper-index/tests
cat > crates/dipper-index/tests/hg_oracle.rs <<'EOF'
use dipper_index::Record;
use std::collections::HashSet;

#[test]
fn record_is_hashable() {
    let mut set = HashSet::new();
    set.insert(Record { identifier: "a".into(), ..Default::default() });
    set.insert(Record { identifier: "a".into(), ..Default::default() });
    assert_eq!(set.len(), 1);
}
EOF
cargo test -p dipper-index --test hg_oracle --quiet
rc=$?
rm -f crates/dipper-index/tests/hg_oracle.rs
exit $rc
"""},
    {"name": "add_method", "preload": [IDX],
     "prompt": "In crates/dipper-index/src/lib.rs, add a public method to `Catalogue` with "
               "the signature `pub fn has(&self, identifier: &str) -> Result<bool>`, which "
               "returns whether `get` finds an item with that identifier.",
     "oracle_cmd": r"""
mkdir -p crates/dipper-index/tests
cat > crates/dipper-index/tests/hg_oracle.rs <<'EOF'
use dipper_index::{Catalogue, Record};

#[test]
fn has_reports_presence() {
    let cat = Catalogue::in_memory().unwrap();
    let mut w = cat.writer().unwrap();
    w.upsert(&Record { identifier: "nasa-apollo".into(), ..Default::default() }).unwrap();
    w.commit().unwrap();
    assert!(cat.has("nasa-apollo").unwrap());
    assert!(!cat.has("nothing-here").unwrap());
}
EOF
cargo test -p dipper-index --test hg_oracle --quiet
rc=$?
rm -f crates/dipper-index/tests/hg_oracle.rs
exit $rc
"""},
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
    ap.add_argument("--brief", default=None,
                    help="project brief file; free-form mode generates a "
                         "structural one from Cargo.toml if omitted")
    ap.add_argument("--no-brief", action="store_true",
                    help="run without the project brief, for the index-value A/B")
    ap.add_argument("--out", default=None, help="write the result JSON here")
    ap.add_argument("--transcript", default=None, help="append a per-turn JSONL event log here")
    ap.add_argument("-p", "--prompt", default=None,
                    help="free-form mode: one ad-hoc task against a real repo")
    ap.add_argument("--scip", default=None,
                    help="path to an index.scip; required by --propagate")
    ap.add_argument("--resample", action="store_true",
                    help="RFC-0003 §4: re-roll a turn that would stall, at a higher "
                         "temperature, before spending the refusal")
    ap.add_argument("--propagate", action="store_true",
                    help="RFC-0003 §3 mechanical propagation. UNSAFE until M1: the "
                         "lexical reference set renamed 84 unrelated sites on dipper")
    ap.add_argument("--work", default="/tmp/hg-work",
                    help="where the overlay and the output patch go")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS,
                    help="turn cap per task; RFC-0003 §6 measures 12 against 24")
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

    if a.prompt:
        print(f"model={MODEL} host={HOST} repo={repo}")
        return free_form(a, repo)

    suite_brief = Path(a.brief) if a.brief else HERE / "dipper-brief.md"
    brief = "" if a.no_brief else (suite_brief.read_text()
                                   if suite_brief.exists() else "")
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
                         transcript=a.transcript, max_turns=a.max_turns,
                         propagate=a.propagate, resample=a.resample, scip=a.scip)
            r["trial"] = trial
            results.append(r)
            print("   ", json.dumps(r), flush=True)

    # Every task resets the repo before it runs, so the final task's edits were
    # being left behind in a checkout someone else then uses. Put it back.
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "clean", "-fdq"], cwd=repo, capture_output=True)

    ep = sum(r["edit_actions"] for r in results)
    ea = sum(r["edits_applied"] for r in results)
    eo = sum(r["edits_ok_first_apply"] for r in results)
    wo = sum(r["wfa"][0] for r in results)
    wt = sum(r["wfa"][1] for r in results)
    all_turns = sorted(s for r in results for s in r["turn_s"])
    med_turn = all_turns[len(all_turns) // 2] if all_turns else 0
    summary = {
        "model": MODEL, "host": HOST, "phases": a.phases, "brief": not a.no_brief,
        "max_turns": a.max_turns, "propagate": a.propagate, "resample": a.resample,
        "trials": a.trials,
        "repo": str(repo), "repo_rev": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
            text=True).stdout.strip(),
        "wfa": [wo, wt],
        "facp_applied": [eo, ea], "facp_proposed": [eo, ep],
        "oracle": [sum(1 for r in results if r["oracle_ok"]), len(results)],
        "oracle_ci": [round(x, 4) for x in
                      wilson(sum(1 for r in results if r["oracle_ok"]), len(results))],
        "median_turn_s": med_turn,
        "total_wall_s": round(sum(r["wall_s"] for r in results), 1),
        "results": results,
    }
    print("\n" + "=" * 66)
    print(f"WFA    {rate(wo, wt)}")
    print(f"FACP   {rate(eo, ea)}  of edits that applied")
    print(f"       {rate(eo, ep)}  of edits proposed")
    print(f"oracle {rate(summary['oracle'][0], len(results))}  <- the quality number (§12)")
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
