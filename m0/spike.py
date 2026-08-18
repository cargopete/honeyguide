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
ASK_SYSTEM = (REPO_ROOT / "prompts" / "ask.md").read_text()
ASK_SCHEMA = json.loads((REPO_ROOT / "prompts" / "ask-schema.json").read_text())
# The last turn of an ask session, with the tool enum narrowed to one value.
# Asked in words to stop reading and answer, the model searched again; the enum
# is the only instruction Ollama's decoder actually enforces, so the final turn
# is expressed as a schema rather than as a sentence.
ASK_FINAL_SCHEMA = json.loads(json.dumps(ASK_SCHEMA))
ASK_FINAL_SCHEMA["properties"]["tool"]["enum"] = ["answer"]

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


# ------------------------------------------------- escalation (RFC-0003 §5)

ESCALATE_MODEL = os.environ.get("HG_ESCALATE_MODEL", "sonnet")

ESCALATE_PROMPT = """\
You are being asked for one edit, by an automated harness. A small local model \
is driving a coding task and has become stuck. Do not explain, do not \
investigate, do not use tools. Reply with one fenced json block and nothing else.

The task it was given:
{task}

The file it is editing: {path}

What it last tried, which did not work:
--- search ---
{search}
--- replace ---
{replace}

Why it failed:
{why}

The relevant region of the file as it stands on disk right now:
{region}

Reply with exactly this shape, where `search` appears **verbatim and exactly \
once** in the region above, and `replace` is what it should become:

```json
{{"search": "...", "replace": "..."}}
```
"""

FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def escalate(task_prompt, path, search, replace, why, region, model=None,
             timeout=180):
    """Hand one stuck edit to the strong model. RFC-0003 §5.2: one edit, never
    the task, because RFC-0002 §2 established that cost scales with
    `turns x context` and a single-turn request carrying a few hundred lines is
    the cheap corner of that product.

    Containment per RFC-0002 §4: `Read Grep Glob` and no write capability, so
    the reply is advice and the harness applies it through the ordinary
    unique-match check and the ordinary gate. An escalated edit is not
    privileged; the strong model is better, not trusted.
    """
    prompt = ESCALATE_PROMPT.format(task=task_prompt, path=path, search=search,
                                    replace=replace, why=why, region=region)
    try:
        p = subprocess.run(
            ["claude", "-p", "--model", model or ESCALATE_MODEL,
             "--output-format", "json", "--allowed-tools", "Read", "Grep", "Glob"],
            input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "escalation timed out"
    if p.returncode != 0:
        return None, f"claude -p failed: {(p.stderr or '').strip()[:160]}"
    try:
        envelope = json.loads(p.stdout)
        body = envelope.get("result") or ""
        cost = envelope.get("total_cost_usd")
    except Exception:
        return None, "claude -p emitted nothing parseable"

    m = FENCE.search(body) or re.search(r"(\{.*\})", body, re.S)
    if not m:
        return None, "no json block in the reply"
    try:
        edit = json.loads(m.group(1))
    except Exception:
        return None, "the json block did not parse"
    if not edit.get("search") or not edit.get("replace"):
        return None, "the reply carried no usable search/replace"
    edit["_cost_usd"] = cost
    return edit, None


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

    # What to tell a model that has read a file to the end, and what to add to a
    # stall refusal. Both are mode-specific: telling a model to edit something in
    # a mode with no edit tool is advice it cannot take, and it was observed
    # taking the only other action available, which was to read the file again.
    EXHAUSTED_HINT = "Reading it again will not help; edit it."
    STALL_HINT = ""
    # Lines served by one `read`, and the hard cap on any observation. A turn
    # costs about fifteen seconds of latency whatever it carries, so a small page
    # spends a whole turn to move very little: 120 lines meant eight turns and
    # 105 seconds to get through an 856-line file. Ask mode raises both, because
    # it pays for context once and pays for turns over and over.
    READ_WINDOW = 120
    OBS_LIMIT = OBS_CAP

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
                                   f"{path}. {self.EXHAUSTED_HINT}")
        e = min(len(lines), end or min(len(lines), s + self.READ_WINDOW))
        self.read_paths.add(str(p))
        self.read_end[str(p)] = max(self.read_end.get(str(p), 0), e)
        body = "\n".join(f"{i:>4}| {lines[i-1]}" for i in range(s, e + 1))
        return f"read {path} lines {s}-{e} of {len(lines)}\n{body}"[:self.OBS_LIMIT]

    def search(self, query):
        # Stands in for scip.sqlite until hg-index exists. Structural enough for
        # a spike: symbol-ish grep, capped at 20 hits. Falls back to grep because
        # ripgrep is not always a real binary on the PATH.
        # `*.toml` and `*.md` as well as `*.rs`. Measured: asked about the web
        # UI, the model searched for `dipper-web`, which is a directory name and
        # a Cargo.toml key and never an identifier in a Rust file, so an honest
        # `no hits` sent it nowhere. Crate names are hyphenated and code is not.
        globs = ("*.rs", "*.toml", "*.md")
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-m", "20"]
            for g in globs:
                cmd += ["--glob", g]
            cmd += [query, "."]
        else:
            cmd = ["grep", "-rn"] + [f"--include={g}" for g in globs] + \
                  ["--exclude-dir=target", "--exclude-dir=.git", query, "."]
        p = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True)
        hits = [h.lstrip("./") for h in (p.stdout or "").strip().splitlines()[:20]]
        if not hits:
            # A search that fails should say what it can see. The alternative is
            # a model with a plausible name, no hits, and no next move, which is
            # exactly the stall this harness exists to remove.
            near = self.near_names(query)
            return (f"search {query!r}: no hits"
                    + (f". Paths whose name is close: {near}" if near else ""))
        return f"search {query!r}: {len(hits)} hits\n" + "\n".join(hits)[:OBS_CAP]

    def near_names(self, query, limit=8):
        """Files and directories whose name resembles the query, hyphens and
        underscores ignored. `dipper-web` finds `crates/dipper-web/`, which is
        the answer the model was actually looking for."""
        key = re.sub(r"[^a-z0-9]", "", query.lower())
        if len(key) < 4:
            return ""
        out = []
        for root, dirs, files in os.walk(self.repo):
            dirs[:] = [d for d in dirs if d not in ("target", ".git", "node_modules")]
            for name in dirs + files:
                flat = re.sub(r"[^a-z0-9]", "", Path(name).stem.lower())
                if flat and (key in flat or flat in key):
                    rel = str((Path(root) / name).relative_to(self.repo))
                    out.append(rel + ("/" if name in dirs else ""))
        return ", ".join(sorted(set(out), key=len)[:limit])

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
                               "Do not repeat it; your next action must be different."
                               + self.STALL_HINT)
        self.stalls = 0
        self.recent.append(sig)

        if missing:
            return self.refuse("missing_args",
                               f"`{tool}` needs {', '.join(missing)}; you sent none of them")

        return self.act(a)

    def act(self, a):
        """The tool-specific half of dispatch. Separate from the preconditions
        above so that a mode with a different tool surface (`AskSession`) gets
        the stall rule, the missing-args rule and the paging behaviour without a
        second copy of any of them."""
        tool = a.get("tool")
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
             scip=None, escalate_to=None):
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
    last_edit = last_obs = None
    escalated = False
    escalations_used = 0
    escalation_cost = 0.0
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
            if a.get("tool") == "edit" and (a.get("search") or "").strip():
                last_edit, last_obs = a, obs
            if obs is None:
                break
            if obs.endswith("ABORT"):
                # RFC-0003 §5.3: a stall abort is one of the deterministic
                # triggers. Hand over the single stuck edit, once, then carry on
                # with the local model driving.
                if escalate_to and not escalated and last_edit:
                    escalated = True
                    ep = (repo / last_edit["path"])
                    region = ""
                    if ep.is_file():
                        body = ep.read_text(errors="replace")
                        anchor = body.find((last_edit.get("search") or "")[:60])
                        if anchor < 0:
                            anchor = 0
                        lo = max(0, body.rfind("\n", 0, max(0, anchor - 2000)))
                        region = body[lo:anchor + 4000]
                    if verbose:
                        print(f"          -> escalating to {escalate_to} ...", flush=True)
                    fix, why = escalate(task["prompt"], last_edit["path"],
                                        last_edit.get("search", ""),
                                        last_edit.get("replace", ""),
                                        (last_obs or "")[:1200], region,
                                        model=escalate_to)
                    rec(kind="escalation", turn=turns, ok=bool(fix),
                        why=why, cost=(fix or {}).get("_cost_usd"))
                    if fix:
                        escalation_cost += (fix.get("_cost_usd") or 0.0)
                        s.recent.clear()          # the abort is spent; let it move
                        s.stalls = 0
                        obs = s.edit(last_edit["path"], fix["search"], fix["replace"])
                        escalations_used += 1
                        if verbose:
                            print(f"          -> escalated edit: {obs.splitlines()[0][:100]}")
                        msgs.append({"role": "user", "content":
                                     "A stronger model was consulted and its edit was applied "
                                     "by the harness. Result:\n" + obs[:OBS_CAP]})
                        continue
                    if verbose:
                        print(f"          -> escalation failed: {why}")
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
        "escalations": escalations_used, "escalation_cost_usd": round(escalation_cost, 4),
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


def session_log_path(kind, repo: Path, text):
    """Where this run's transcript goes when `--transcript` is not given.

    Every session is logged by default rather than on request. The event log is
    already the telemetry and the eval corpus (§4.1), and a corpus only exists if
    it is collected without anyone remembering to ask for it. One file per run,
    named so the directory can be read without opening anything."""
    d = Path(os.environ.get("HG_LOG_DIR", Path.home() / ".honeyguide" / "sessions"))
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (text or kind).lower()).strip("-")[:48] or kind
    model = re.sub(r"[^a-z0-9]+", "-", MODEL.lower()).strip("-")
    return d / f"{time.strftime('%Y%m%d-%H%M%S')}-{model}-{repo.name}-{kind}-{slug}.jsonl"


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
                 propagate=a.propagate, resample=a.resample, scip=a.scip,
                         escalate_to=a.escalate)

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


# ------------------------------------------------------------------ ask mode
#
# Read-only Q&A over a repository, and the first thing to say about it is what
# it does not have. §6.2's compile gate is the mechanism this design leans on,
# and an answer cannot be compiled. Nothing here can tell a true sentence from a
# false one.
#
# What it can do is check the *names*. The failure this project was founded on
# was a schema-perfect edit against an entirely invented `src/lib.rs`, and in
# edit mode that dies against unique-match search. In ask mode the same
# fabrication arrives as confident prose, so the harness checks every backticked
# name and every path in the answer against a vocabulary built from the files on
# disk, and refuses an answer that mentions something which is not there. That
# is weaker than the compile gate by a long way. It is deterministic, it costs
# zero model tokens, and it is aimed at the one failure mode actually observed.
#
# The mode is read-only at the decoder rather than at dispatch: `prompts/ask-
# schema.json` has three tools in its `enum` and `edit` is not one of them.

DECLINED = {"none", "n/a", "na", "-", "", "null"}

# Names a Rust answer may legitimately use without them appearing in this
# repository. Deliberately short: third-party crates do not need to be here
# because the vocabulary includes every `Cargo.toml`, so a mention of `tokio` in
# a project that does not depend on tokio *should* be refused.
STD_NAMES = {
    "self", "crate", "super", "impl", "struct", "enum", "trait", "match", "async",
    "await", "unsafe", "static", "const", "type", "where", "return", "break",
    "continue", "while", "loop", "move", "pub", "mut", "ref", "let", "true",
    "false", "none", "some", "bool", "char", "usize", "isize", "u128", "i128",
    "f32", "f64", "vec", "string", "option", "result", "box", "arc", "rc",
    "refcell", "mutex", "rwlock", "hashmap", "hashset", "btreemap", "btreeset",
    "vecdeque", "path", "pathbuf", "iterator", "intoiterator", "display",
    "debug", "clone", "copy", "default", "from", "into", "tryfrom", "tryinto",
    "send", "sync", "sized", "drop", "deref", "error", "ordering", "cow",
    "cargo", "rustc", "clippy", "rustfmt", "rust", "toml", "json", "http",
    "https", "todo", "unwrap", "expect", "main", "test", "tests", "std", "core",
    "alloc", "dyn", "fn", "mod", "use", "workspace", "readme", "license",
}

BACKTICKED = re.compile(r"`([^`\n]{1,80})`")
# Measured, and it was the hole in the first version of this check: asked about a
# feature that does not exist, the model invented `MseCrypto` and
# `PeerConnection::connect()` and wrote them in **bold** and bare, not in
# backticks, so a backtick-only scan passed them both. Prose is not a format the
# model has agreed to; the check has to read it as it is actually written.
BOLD = re.compile(r"\*\*([^*\n]{1,80})\*\*")
# CamelCase with at least one lowercase letter in the first hump, so `RC4`,
# `HTTP` and `API` are not candidates and `MseCrypto` is. Acronyms are excluded
# deliberately: they are ordinary English in a sentence about a protocol.
CAMEL = re.compile(r"\b[A-Z][a-z0-9_]+(?:[A-Z][a-z0-9_]*)+\b")
SCREAMING = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b")
FILEISH = re.compile(r"\b[\w./-]+\.(?:rs|toml)\b")
CITE = re.compile(r"([\w][\w./-]*\.(?:rs|toml))\s*:\s*(\d+)(?:\s*-\s*(\d+))?")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
IDENT_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _source_files(repo: Path, pats=("*.rs", "*.toml")):
    """Every file the citation checker considers real. `target` is excluded
    because a build directory contains generated sources, and an answer citing
    one of those is citing something the user cannot read."""
    for pat in pats:
        for f in repo.rglob(pat):
            parts = f.relative_to(repo).parts
            if "target" in parts or ".git" in parts:
                continue
            yield f


def repo_vocabulary(repo: Path):
    """(identifiers, relative paths). Built once per session by scanning the
    tree: 14.7k lines takes about a tenth of a second, and it turns every
    fabrication check into a set membership rather than a grep.

    The identifier half also reads `*.md`, and the citable-file half does not.
    A name that appears in the README is not one the model invented, even if no
    Rust file contains it, and refusing "dipper is a BitTorrent client" because
    `BitTorrent` is not an identifier would be a false refusal on a true
    sentence. Citations still have to point at code."""
    idents, files = set(), []
    for f in _source_files(repo):
        files.append(str(f.relative_to(repo)))
    for f in _source_files(repo, ("*.rs", "*.toml", "*.md")):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        idents.update(w.lower() for w in WORD.findall(text))
        idents.add(f.stem.lower())
    return idents, files


def _looks_like_code(tok: str) -> bool:
    """Whether an un-backticked token is claiming to be a name at all.

    Measured, and it refused a correct answer: asked how resume data is saved,
    the model wrote `**Loading:**` and `**Saving:**` as section headings, and a
    scan that treats every bold word as a symbol called both of them
    fabrications. Bold is emphasis in prose and a name in code, and only the
    second is checkable, so an un-backticked candidate has to look like code
    before it is held to code's standard. Backticked names keep the old rule,
    because backticks are the format the prompt asked names to arrive in."""
    return bool("::" in tok or "(" in tok or "_" in tok
                or FILEISH.fullmatch(tok) or CAMEL.fullmatch(tok))


def _symbol_parts(tok: str):
    """`Catalogue::count()` -> ['Catalogue', 'count']. Strips the decorations a
    model puts round a name in prose: call parens, `!` on macros, `?`, borrows,
    generic arguments, and trailing punctuation."""
    tok = tok.strip().strip(",.;:")
    tok = tok.split("<")[0]
    tok = tok.lstrip("&*")
    tok = re.sub(r"\(.*$", "", tok).rstrip("!?()")
    return [p for p in re.split(r"::|\.", tok) if p]


def ungrounded_terms(text: str, idents, files):
    """Names in an answer that do not occur anywhere in the repository.

    Conservative on purpose, because a false refusal costs a turn and a turn is
    expensive: only single-token backticked names of four characters or more are
    checked, so `cargo check --workspace` and ordinary English are never
    candidates. That leaves exactly the shape the observed fabrication took, a
    confident type or function name that does not exist."""
    bad = []
    candidates = [m.group(1).strip() for m in BACKTICKED.finditer(text)]
    candidates += [t for t in (m.group(1).strip().rstrip(":") for m in BOLD.finditer(text))
                   if _looks_like_code(t)]
    candidates += [m.group(0) for m in CAMEL.finditer(text)]
    candidates += [m.group(0) for m in SCREAMING.finditer(text)]
    for tok in candidates:
        if not tok or any(c.isspace() for c in tok):
            continue                        # a phrase or a command, not a name
        if FILEISH.fullmatch(tok):
            continue                        # handled with the other paths below
        for part in _symbol_parts(tok):
            if not IDENT_OK.match(part) or len(part) < 4:
                continue
            if part.lower() in STD_NAMES or part.lower() in idents:
                continue
            bad.append(part)
    for m in FILEISH.finditer(text):
        rel = m.group(0).lstrip("./")
        if not any(f == rel or f.endswith("/" + rel) for f in files):
            bad.append(rel)
    return sorted(dict.fromkeys(bad))


def resolve_cited_path(cand: str, files):
    """A citation's path, resolved against the tree. Accepts a bare file name
    when it is unambiguous, because insisting on the full path costs a turn to
    say so and the harness can tell whether it is ambiguous."""
    cand = cand.lstrip("./")
    exact = [f for f in files if f == cand]
    if exact:
        return exact[0], None
    tail = [f for f in files if f.endswith("/" + cand)]
    if len(tail) == 1:
        return tail[0], None
    if len(tail) > 1:
        return None, f"{cand} matches {len(tail)} files; give the path from the repository root"
    return None, f"there is no {cand} in this repository"


def parse_citations(repo: Path, raw: str, files):
    """(cites, problems). Each cite is (rel, lo, hi, [lines]), and the lines are
    read from disk by the harness rather than taken from the model: the point of
    a citation is that the user can check the claim against the file, so the
    quoted text has to come from the file."""
    cites, problems = [], []
    for m in CITE.finditer(raw or ""):
        rel, why = resolve_cited_path(m.group(1), files)
        if why:
            problems.append(why)
            continue
        lines = (repo / rel).read_text(errors="replace").splitlines()
        lo = int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else lo
        if lo > len(lines):
            problems.append(f"{rel} has {len(lines)} lines, so {rel}:{lo} does not exist")
            continue
        hi = max(lo, min(hi, len(lines), lo + 5))
        cites.append((rel, lo, hi, lines[lo - 1:hi]))
    return cites, problems


class AskSession(Session):
    """One question against one repository, read-only.

    Inherits `read`, `search`, the stall rule and the paging behaviour from
    `Session` and adds no way to write anything. The gate command it is given
    would fail loudly if some later edit to this file ever called it."""

    REQUIRED = {"read": ["path"], "search": ["query"], "answer": ["answer"]}
    SIG_FIELDS = {"read": ("path", "start", "end"), "search": ("query",),
                  "answer": ("answer",)}
    READ_WINDOW = 240
    OBS_LIMIT = 8000
    EXHAUSTED_HINT = ("Reading it again will not help. Read a different file, "
                      "`search` for a name, or answer from what you have.")
    STALL_HINT = (" If what you have read does not answer the question, say so in "
                  "`answer` and set `citations` to `none`.")

    def __init__(self, repo: Path, vocab):
        super().__init__(repo, Path("/nonexistent"), "false")
        self.idents, self.files = vocab
        self.seen = set()           # paths read, or returned by a search, this session
        self.supplied = set()       # absolute paths retrieval already supplied in full
        # Paths a carried answer already cited. Measured: told it had not read
        # `picker.rs` this session, the model re-sent the same citation six times
        # rather than reading the file, because from where it sits the thread
        # plainly did read it. Those paths are citable again; every other path
        # still has to be read. The provenance is printed either way, since a
        # citation the model is quoting from memory is worth less than one it is
        # looking at, and the reader should be told which is which.
        self.carried = set()
        self.answers = 0
        self.result = None          # (text, cites, declined)

    def _rel(self, path):
        p = self._abs(path)
        if p is None or not p.is_file():
            return None
        try:
            return str(p.relative_to(self.repo.resolve()))
        except ValueError:
            return None

    def read(self, path, start=None, end=None, advance=False):
        # Measured: asked about the piece picker, retrieval supplied all 498
        # lines of `picker.rs` and the model then read it from line 1 in five
        # pages of twenty seconds each, learning nothing it had not been given.
        # A rangeless read of a file it already holds in full is treated as a
        # page forward, which is what the stall rule does four turns later
        # anyway, and it arrives at the same place having spent nothing.
        p = self._abs(path)
        if p is not None and str(p) in self.supplied and not start and not end:
            advance = True
        obs = super().read(path, start, end, advance)
        rel = self._rel(path)
        if rel and not obs.startswith("REFUSED"):
            self.seen.add(rel)
        return obs

    def search(self, query):
        obs = super().search(query)
        for line in obs.splitlines()[1:]:
            rel = line.split(":", 1)[0]
            # A grep hit shows the matching line, so the model has seen enough of
            # that file to cite the line it was shown, and only that line.
            if rel.endswith(".rs"):
                self.seen.add(rel)
        return obs

    def answer(self, text, citations):
        """The grounding gate. Returns None to end the session, or a refusal."""
        self.answers += 1
        text = (text or "").strip()
        if not text:
            return self.refuse("empty_answer", "`answer` was empty; say what you found.")

        declined = (citations or "").strip().lower() in DECLINED
        if declined:
            # A declared non-answer asserts nothing about the repository, so
            # there is nothing to check and nothing to refuse. Refusing one would
            # only teach the model to guess instead, which is the trade this
            # whole mode exists to avoid.
            self.result = (text, [], True)
            return None

        cites, problems = parse_citations(self.repo, citations, self.files)
        if not cites and not problems:
            problems.append("`citations` must be `path:line` or `path:start-end`, comma "
                            "separated, or the single word `none` if you cannot answer")
        unseen = sorted({rel for rel, *_ in cites if rel not in self.seen})
        carried_unread = [r for r in unseen if r in self.carried]
        for rel in (r for r in unseen if r not in self.carried):
            problems.append(f"you cited {rel}, which you have not read this session; "
                            "read it, or cite a file you have")

        # A carried path is one an earlier answer in this thread cited, so simply
        # allowing it was the obvious move and it was wrong. Measured: permitted
        # to cite `picker.rs` from memory, the model answered that the endgame
        # returns every unrequested piece, named a `next` method that does not
        # exist, and cited line 1, the module doc comment. The real behaviour is
        # that endgame permits a *duplicate* request for a piece already in
        # flight, which is one filter in `next_for`. It was recalling, not
        # reading, and recall is exactly what this harness does not accept
        # anywhere else. So satisfy the precondition instead: hand over the
        # region it claims to be citing and make it answer from the text.
        if carried_unread and not problems:
            rel = carried_unread[0]
            lo = min(c[1] for c in cites if c[0] == rel)
            body = self.read(rel, max(1, lo - 40), lo + 80)
            return (f"Your answer was not accepted: you cited {rel} from an earlier "
                    "session but have not read it in this one, so it was written from "
                    "memory rather than from the file. Here is that part of it. Answer "
                    "again from what it actually says.\n\n" + body)
        bad = ungrounded_terms(text, self.idents, self.files)
        if bad:
            problems.append("these names in your answer do not appear anywhere in this "
                            "repository: " + ", ".join(bad) + "; they were not read, they "
                            "were invented, so remove them and answer from what you read")
        if problems:
            return self.refuse("ungrounded_answer", "; ".join(problems))

        # Fifth element: whether this citation rests on something read in this
        # session, or on a file the thread read in an earlier one.
        self.result = (text, [(rel, lo, hi, lines, rel in self.seen)
                              for rel, lo, hi, lines in cites], False)
        return None

    def act(self, a):
        tool = a.get("tool")
        if tool == "read":
            return self.read(a["path"], a.get("start"), a.get("end"))
        if tool == "search":
            return self.search(a["query"])
        return self.answer(a.get("answer", ""), a.get("citations", ""))


def run_ask(question, repo: Path, brief, preload, preload_paths, max_turns,
            transcript=None, verbose=True, carry=(), attached=()):
    idents, files = repo_vocabulary(repo)
    # Names the user supplied are not names the model invented, so the grounding
    # check has to know about them. Without this every identifier in a pasted
    # snippet reads as a fabrication and the answer is refused for quoting the
    # question back. The attachment cannot be *cited*, though: citations point at
    # files on disk, and a paste is not one.
    for _, text in attached:
        idents.update(w.lower() for w in WORD.findall(text))
    s = AskSession(repo, (idents, files))
    # Which of the preloaded files were supplied whole rather than as the regions
    # around a match. Only a whole file can be treated as already read; claiming
    # that for a file supplied in fragments would hide the parts it never saw.
    whole = dict(re.findall(r"^(\S+\.rs) \((\d+) lines, complete\)", preload, re.M))
    for rel in preload_paths:
        f = (repo / rel)
        if f.is_file():
            s.read_paths.add(str(f.resolve()))
            s.seen.add(rel)
            if rel in whole:
                s.supplied.add(str(f.resolve()))
                s.read_end[str(f.resolve())] = int(whole[rel])

    # The earlier questions, so a follow-up has a referent, and nothing else.
    # What those answers *said* is not carried; the files they came from are
    # supplied as context instead, so the model reads rather than recalls.
    thread = ""
    if carry:
        for e in carry:
            s.carried.update(e.get("paths", ()))
        thread = ("\n\nEarlier questions in this thread, so that a follow-up has "
                  "something to refer back to. The answers are not repeated here; read "
                  "the files below.\n"
                  + "\n".join(f"  - {e['question']}" for e in carry))
    supplied = ""
    for name, text in attached:
        supplied += (f"\n\nText supplied with the question, from {name}. This is not part "
                     "of the repository and cannot be cited; use it to understand what is "
                     f"being asked.\n---\n{text}\n---")

    msgs = [{"role": "system", "content": ASK_SYSTEM + ("\n\n" + brief if brief else "")},
            {"role": "user", "content": "Question: " + question + supplied + thread + preload}]

    def rec(**kw):
        if transcript:
            with open(transcript, "a") as fh:
                fh.write(json.dumps(kw) + "\n")

    rec(kind="ask", model=MODEL, host=HOST, repo=str(repo), question=question,
        at=time.strftime("%Y-%m-%dT%H:%M:%S"), retrieved=list(preload_paths),
        carried=[e["question"] for e in carry],
        attached=[{"from": n, "chars": len(t)} for n, t in attached],
        preload_lines=preload.count("\n"), brief_bytes=len(brief or ""),
        max_turns=max_turns)

    wfa_ok = wfa_total = 0
    turn_s, turns = [], 0
    last_answer = None
    forced = False
    t0 = time.time()
    for turns in range(1, max_turns + 1):
        t_turn = time.time()
        try:
            wfa_total += 1
            r = call(msgs, schema=ASK_SCHEMA, num_predict=900)
            raw = (r.get("message") or {}).get("content") or ""
            try:
                a = json.loads(raw)
                wfa_ok += 1
            except Exception:
                rec(kind="turn", turn=turns, malformed=raw[:2000])
                msgs.append({"role": "user",
                             "content": "That was not valid JSON. Emit one action object."})
                continue

            msgs.append({"role": "assistant", "content": raw})
            if verbose:
                shown = {"read": "path", "search": "query"}.get(a.get("tool"), "")
                print(f"    [{turns}] {a.get('tool')} "
                      f"{a.get(shown) if shown else ''}"[:110], flush=True)
            if a.get("tool") == "answer":
                last_answer = a
            obs = s.dispatch(a)
            rec(kind="turn", turn=turns, action=a, obs=obs)
            if obs is None:
                break
            if obs.endswith("ABORT"):
                if verbose:
                    print("          -> stalled three times on the same action")
                break
            if verbose:
                print(f"          -> {obs.splitlines()[0][:110]}", flush=True)
            msgs.append({"role": "user", "content": obs[:s.OBS_LIMIT]})
        finally:
            turn_s.append(round(time.time() - t_turn, 1))

    # The loop ending is a fact about the loop, not about what the model knows.
    # A model that has read four hundred lines and then run out of turns is one
    # question away from being able to say so, and that question costs one turn.
    if not s.result and turns:
        if verbose:
            print("    [forced] out of turns; asking for an answer from what was read",
                  flush=True)
        s.recent.clear()
        s.stalls = 0
        msgs.append({"role": "user", "content":
                     "Stop reading. Answer the question now from what you have already "
                     "been shown this session. If it is not enough, say exactly that in "
                     "`answer` and set `citations` to `none`."})
        # Two attempts, not one. Measured: forced to answer a question about a
        # feature the repository does not have, the model invented a struct, two
        # files and a constant. The grounding check refused all four by name, and
        # a model told exactly which names do not exist is in a position to say
        # the thing it should have said first. A third attempt is not offered:
        # past two this is no longer a model that needs telling.
        for attempt in (1, 2):
            wfa_total += 1
            t_turn = time.time()
            r = call(msgs, schema=ASK_FINAL_SCHEMA, num_predict=900)
            turn_s.append(round(time.time() - t_turn, 1))
            raw = (r.get("message") or {}).get("content") or ""
            try:
                a = json.loads(raw)
            except json.JSONDecodeError:
                rec(kind="turn", turn=turns + attempt, forced=True, malformed=raw[:2000])
                break
            wfa_ok += 1
            forced = True
            last_answer = a
            obs = s.dispatch(a)
            rec(kind="turn", turn=turns + attempt, forced=True, action=a, obs=obs)
            if verbose:
                print(f"          -> {(obs or 'answered').splitlines()[0][:110]}", flush=True)
            if obs is None:
                break
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": obs[:OBS_CAP]})
            s.recent.clear()
            s.stalls = 0

    out = {
        "turns": turns, "wall_s": round(time.time() - t0, 1), "turn_s": turn_s,
        "median_turn_s": sorted(turn_s)[len(turn_s) // 2] if turn_s else None,
        "wfa": (wfa_ok, wfa_total), "answers": s.answers, "forced": forced,
        "grounded": bool(s.result and not s.result[2]),
        "declined": bool(s.result and s.result[2]),
        "result": s.result, "last_answer": last_answer, "refusals": s.refusals,
    }
    rec(kind="result", question=question,
        **{k: v for k, v in out.items() if k not in ("result", "last_answer")},
        answer=(s.result[0] if s.result else None),
        citations=([[rel, lo, hi] for rel, lo, hi, *_ in s.result[1]] if s.result else []),
        ungrounded_attempt=(None if s.result else (last_answer or {}).get("answer")))
    # The exact conversation, kept whole. Per-turn records say what happened;
    # tuning a prompt needs what the model was actually looking at when it did.
    rec(kind="messages", messages=msgs)
    return out


OUTLINE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?"
                     r"(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\S+\s+)?"
                     r"(fn|struct|enum|trait|impl|mod|type|static)\s")


def outline(path: Path, limit=90):
    """The declarations in a file, with their line numbers.

    For a file too big to supply whole, retrieval gives the regions around the
    matches and ends them with "use `read` for the rest". Measured, the model
    took that literally: eight pages and 105 seconds to walk an 856-line
    `main.rs` from the top. An outline is what it actually needed, and it is
    §5.3's "symbol signatures" done lexically, sixty lines instead of eight
    hundred, so the next `read` can be a range rather than a march."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    rows = [f"{i:>4}| {l.strip()[:110]}" for i, l in enumerate(lines, 1)
            if OUTLINE.match(l)]
    if not rows:
        return ""
    head = (f"\nThe declarations in this file, so you can `read` the range you want "
            f"instead of paging from the top ({len(rows)} of {len(lines)} lines):")
    if len(rows) > limit:
        rows = rows[:limit] + [f"  ... {len(rows) - limit} more declarations"]
    return head + "\n" + "\n".join(rows)


def file_map(repo: Path, budget=70):
    """A listing of the Rust files and their sizes, for when retrieval finds
    nothing at all.

    §5.1's `repomap.txt` is the real answer to this and it is PageRank-ranked and
    budgeted. This is the version that costs nothing and needs no index: a
    question like "explain the UI" contains no symbol, retrieval returns empty,
    and the model starts with no idea which files exist. It then guesses a name,
    finds nothing, and stalls. A plain list of what is there is not clever, but
    it is the difference between searching and guessing."""
    rows = []
    for f in _source_files(repo, ("*.rs",)):
        try:
            n = len(f.read_text(errors="replace").splitlines())
        except OSError:
            continue
        rows.append((str(f.relative_to(repo)), n))
    if not rows:
        return ""
    rows.sort(key=lambda r: -r[1])
    shown = rows[:budget]
    out = ["\n\nRetrieval matched nothing in your question, so here is every Rust file "
           f"in the repository instead, largest first ({len(rows)} files). Nothing has "
           "been read yet: use `read` on whichever of these looks right."]
    out += [f"  {rel} ({n} lines)" for rel, n in shown]
    if len(rows) > budget:
        out.append(f"  ... and {len(rows) - budget} smaller files")
    return "\n".join(out)


ATTACH_CAP = 6000       # characters; §8.5 budgets ~4k tokens for the whole prompt


def read_attachment(paths, cap=ATTACH_CAP):
    """Text supplied with the question rather than found in the repository: a
    file, or whatever arrives on stdin.

    It exists because the shell is a hostile channel for prose. A pasted error
    log or code sample carries backticks and `$(...)`, and inside a double-quoted
    argument zsh executes both before honeyguide sees a character of it. A path,
    or a pipe, passes the bytes through untouched.

    Truncation is announced rather than silent: a quietly halved attachment looks
    exactly like a model that ignored half of it.

    A named file that is not there is fatal, and the first version only warned.
    Measured, and it is the worst output this mode has produced: `-f
    ~/suggestion.txt "how can we do this in dipper?"` with no such file asked
    "how can we do this" about nothing at all, and got back a fluent paragraph
    saying dipper downloads torrents, correctly cited to a doc comment. Every
    deterministic check passed, because every check tests the answer and the
    fault was in the question. A precondition the harness can test is not left
    to the model to notice."""
    chunks, missing = [], []
    for path in paths or ():
        f = Path(path)
        if not f.is_file():
            missing.append(str(f))
            continue
        chunks.append((str(f), f.read_text(errors="replace")))
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            chunks.append(("stdin", piped))
    out = []
    for name, text in chunks:
        if len(text) > cap:
            print(f"attachment {name}: {len(text)} characters, using the first {cap}")
            text = text[:cap] + "\n... [truncated by the harness]"
        out.append((name, text))
    return out, missing


def carry_context(repo: Path, sessions=3):
    """The earlier questions in this repository's thread, and the files their
    answers came from. Oldest first. For `--continue`.

    The prose of those answers is deliberately **not** carried, and the first
    version of this got that wrong. Carrying the answer text let the model reply
    from memory: asked what the endgame changes, it recalled that the picker
    returns every unrequested piece, named a `next` method that does not exist
    and cited line 1, the module doc comment. The real behaviour is a duplicate
    request permitted for a piece already in flight, one filter in `next_for`.
    Worse, that wrong answer was then carried into the next session and repeated
    verbatim, so a single bad answer becomes the thread's premise.

    So a follow-up inherits two things: the earlier questions, so that "that" and
    "it" have a referent, and the paths those answers were grounded in, which are
    supplied as context so the model reads the code again rather than
    remembering it. Referents from the thread, facts from the file."""
    d = Path(os.environ.get("HG_LOG_DIR", Path.home() / ".honeyguide" / "sessions"))
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.jsonl"), reverse=True):
        if len(out) >= sessions:
            break
        head = res = None
        for line in f.read_text(errors="replace").splitlines():
            if line.startswith('{"kind": "messages"'):
                continue                    # the bulky half, deliberately skipped
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "ask":
                head = r
            elif r.get("kind") == "result":
                res = r
        # Grounded answers only. A decline reads "I have not read any source
        # files", which is true of the session that produced it and false in the
        # one it would be carried into, and an ungrounded attempt is a
        # fabrication the gate has already refused once.
        if (not head or head.get("repo") != str(repo) or not res
                or not res.get("answer") or not res.get("grounded")):
            continue
        cites = res.get("citations") or []
        out.append({"question": head.get("question", ""),
                    "paths": sorted({c[0] for c in cites}), "file": f.name})
    out.reverse()
    return out


def ask(a, question, repo: Path, attached=()):
    """`--ask "..."`. No overlay: there is no tool in this mode that can write,
    so there is nothing to protect the working tree from, and a whole-tree copy
    of a large repository is a second and a half of nothing."""
    brief = "" if a.no_brief else (Path(a.brief).read_text()
                                   if a.brief and Path(a.brief).is_file()
                                   else degraded_brief(repo))
    # Retrieval reads the attachment too: a question of the form "why does this
    # fail" carries no symbol at all, and every symbol worth looking up is in the
    # text that came with it.
    preload, used, _ = retrieve(repo, question + " " + " ".join(t for _, t in attached),
                                max_files=3, max_lines=600)
    if not used:
        preload = file_map(repo)
        print("retrieval found nothing; supplying the file list instead")
    else:
        # Any file supplied in fragments gets its outline as well, so the model
        # has somewhere to aim. Files supplied whole need nothing: it has them.
        whole = set(re.findall(r"^(\S+\.rs) \(\d+ lines, complete\)", preload, re.M))
        for rel in used:
            if rel in whole:
                continue
            body = outline(repo / rel)
            if body:
                preload += f"\n\n{rel}:{body}"
    carry = carry_context(repo) if a.continue_ else []
    if a.continue_:
        if carry:
            for e in carry:
                print(f"carrying: {e['question'][:70]}")
        else:
            print("nothing to carry: no answered session for this repository yet")
    # Whatever the thread was grounded in, supply again. A follow-up is usually
    # about the same file, and paying for it in context is cheaper than the three
    # turns of paging it otherwise costs.
    for rel in dict.fromkeys(p for e in carry for p in e["paths"]):
        f = repo / rel
        if rel in used or not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        if len(lines) > 700:
            continue                        # too big to hand over whole; let it page
        preload += (f"\n\n{rel} ({len(lines)} lines, complete):\n"
                    + "\n".join(f"{i:>4}| {l}" for i, l in enumerate(lines, 1)))
        used.append(rel)
        if len(used) >= 4:
            break
    print("warming model ...", flush=True)
    call([{"role": "user", "content": "ok"}], num_predict=1)

    r = run_ask(question, repo, brief, preload, used, a.max_turns,
                transcript=a.transcript, carry=carry, attached=attached)

    print("\n" + "=" * 66)
    if not r["result"]:
        print(f"no answer in {r['turns']} turns.")
        if r["refusals"]:
            print("refusals:", ", ".join(r["refusals"]))
        if r["last_answer"]:
            # Printed, but never as though it had passed. The last attempt is
            # usually informative about *why* it failed, and hiding it would make
            # the mode harder to debug than it needs to be.
            print("\nthe last answer attempt, WHICH FAILED THE GROUNDING CHECK "
                  "and should not be trusted:\n")
            print(r["last_answer"].get("answer", "")[:1500])
        print("=" * 66)
        return 1

    text, cites, declined = r["result"]
    print(text)
    if declined:
        print("\n(the model declined to answer from what it read, which is a permitted "
              "outcome and a better one than a guess)")
    if cites:
        print("\ncitations, read back from disk by the harness:")
        for rel, lo, hi, lines, fresh in cites:
            if not fresh:
                print(f"  ({rel} was not read in this session; carried from an earlier one)")
            for i, line in enumerate(lines, lo):
                print(f"  {rel}:{i}  {line.strip()[:100]}")
    print("=" * 66)
    print(f"turns {r['turns']}   wall {r['wall_s']}s   median turn {r['median_turn_s']}s"
          f"   answers {r['answers']}")
    if r["refusals"]:
        print("refusals:", ", ".join(r["refusals"]))
    print("checked: every cited path exists and was read this session, every cited line "
          "exists, and every name in the answer occurs in the repository.")
    print("NOT checked: whether the answer is true. There is no compile gate on prose.")
    return 0 if not declined else 1


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
    ap.add_argument("--transcript", default=None,
                    help="append a per-turn JSONL event log here. Defaults to a new file "
                         "under ~/.honeyguide/sessions, or $HG_LOG_DIR: every run is "
                         "logged whether or not anyone asked for it")
    ap.add_argument("-f", "--attach", action="append", metavar="PATH",
                    help="supply a file's text along with the question. Repeatable. "
                         "Text also arrives on stdin when it is piped, which is the safe "
                         "way to pass a paste containing backticks or $(...)")
    ap.add_argument("-c", "--continue", dest="continue_", action="store_true",
                    help="carry the questions and answers from this repository's recent "
                         "ask sessions into this one. File contents are not carried: a "
                         "citation has to be read again to be made again")
    ap.add_argument("--ask", default=None, metavar="QUESTION",
                    help="read-only Q&A: answer a question about the repository. "
                         "No overlay, no edits, no compile gate; answers are checked "
                         "against the names and lines that exist on disk")
    ap.add_argument("-p", "--prompt", default=None,
                    help="free-form mode: one ad-hoc task against a real repo")
    ap.add_argument("--escalate", nargs="?", const=ESCALATE_MODEL, default=None,
                    metavar="MODEL",
                    help="RFC-0003 §5: hand one stuck edit to `claude -p` on a stall "
                         "abort. Defaults to sonnet; costs about $0.13 a call")
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
    if not a.transcript and not a.selftest:
        kind = "ask" if a.ask else ("edit" if a.prompt else "suite")
        a.transcript = str(session_log_path(kind, repo, a.ask or a.prompt))

    if a.selftest:
        print(f"oracle self-test against {repo}")
        bad = selftest(repo, Path(a.target_dir))
        print("all oracles fail on a pristine tree" if not bad else f"{bad} oracle(s) useless")
        return 1 if bad else 0

    if a.ask or a.attach or not sys.stdin.isatty():
        attached, missing = read_attachment(a.attach)
        if missing:
            print("attachment not found: " + ", ".join(missing))
            print("refusing to run: the question was written to be read alongside it")
            return 2
        question = a.ask or ("Explain the text supplied with this question, in the "
                             "context of this repository.")
        if not a.ask and not attached:
            print("nothing to ask: give a question, a --attach file, or pipe text in")
            return 2
        print(f"model={MODEL} host={HOST} repo={repo}")
        print(f"transcript: {a.transcript}")
        if not a.ask:
            print(f"no question given, using: {question}")
        return ask(a, question, repo, attached)

    if a.prompt:
        print(f"model={MODEL} host={HOST} repo={repo}")
        print(f"transcript: {a.transcript}")
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
                         propagate=a.propagate, resample=a.resample, scip=a.scip,
                         escalate_to=a.escalate)
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
        "max_turns": a.max_turns, "propagate": a.propagate, "resample": a.resample, "escalate": a.escalate,
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
