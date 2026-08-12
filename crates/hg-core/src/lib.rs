//! Honeyguide agent core: session state, the ReAct loop, tool dispatch, the
//! compile gate.
//!
//! This crate currently carries the RFC-0001 data model only. The types are the
//! specification: the Submission/Event split (§4.1), the five-tool action
//! surface (§6.1), and the gate outcomes (§6.2). Behaviour lands at M2.

#![forbid(unsafe_code)]

use std::fmt;
use std::path::PathBuf;

/// Identifies one proposed edit within a session. Monotonic from 0.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct EditId(pub u32);

impl fmt::Display for EditId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "edit#{}", self.0)
    }
}

/// Identifies one tool invocation within a session. Monotonic from 0.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CallId(pub u32);

// ---------------------------------------------------------------------------
// §4.1 Submission Queue: intent flowing in, from any frontend.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Submission {
    UserMessage(String),
    Command(SlashCmd),
    /// Cancel the in-flight generation. At ~13 tok/s this is used often enough
    /// to be a first-class input rather than a signal handler.
    Interrupt,
    Approve(EditId),
    Reject(EditId),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SlashCmd {
    /// Refresh `.agent-index/`. `full` skips the incremental path.
    Index { full: bool },
    /// Fold history into a deterministic summary. Invalidates the prefix cache,
    /// so it is explicit rather than automatic. See RFC-0001 §8.5.
    Compact,
    /// Revert the last applied edit via git.
    Undo,
    /// Print the serving-config probe results (RFC-0001 §8.4).
    Model,
    /// v0.2. Hand the stuck task to the strong model.
    Escalate,
}

// ---------------------------------------------------------------------------
// §4.1 Event Queue: everything the core knows, flowing out.
//
// The event stream is the transcript, the telemetry, and the eval corpus. It is
// append-only and is written to session JSONL verbatim. Nothing may reach a
// frontend except through here.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// Phase-1 free generation, streamed token by token.
    Reasoning(String),
    /// Prose addressed to the user (from `finish`, or a degenerate turn).
    AgentText(String),

    ToolCall { id: CallId, action: Action },
    ToolResult { id: CallId, observation: Observation },

    EditProposed { id: EditId, path: PathBuf, unified_diff: String },
    EditApplied { id: EditId },
    EditRejected { id: EditId },
    /// The gate refused the edit. `errors` is rustc output, verbatim, and is
    /// fed back to the model unmodified.
    EditBounced { id: EditId, reason: BounceReason, errors: String },

    RepairAttempt { edit: EditId, n: u8 },
    EscalationSuggested { reason: EscalationTrigger },

    /// Emitted once per model call. `prefill_ms` is the number to watch, not
    /// the token counts: prefill is the binding cost on this hardware, and a
    /// prefix-cache hit is visible only as a collapsed duration (RFC-0001 §8.3).
    TokenUsage { prompt: u32, completion: u32, prefill_ms: u64, wall_ms: u64, cache_hit: bool },

    IndexStale { head_rev: String, index_rev: String, overlap: bool },
    Error(String),
}

// ---------------------------------------------------------------------------
// §6.1 The tool surface. Five variants, hard ceiling.
//
// Every added tool is an added failure mode: past roughly five, Qwen-class MoEs
// abandon the trained call format and leak markup into content (Goose #6883).
// Do not extend this enum without an eval that justifies it.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    /// Read a line range. Also records the path as *seen*, which is the
    /// precondition for editing it (see `BounceReason::PathNotRead`).
    Read { path: PathBuf, start: Option<u32>, end: Option<u32> },
    /// Symbol lookup against `scip.sqlite`, falling back to ripgrep. Capped at
    /// 20 hits. This is what removes the need for `ls` and `cat`.
    Search { query: String },
    /// Propose a search/replace edit. Enters the gate; never touches the working
    /// tree directly.
    Edit { path: PathBuf, search: String, replace: String },
    /// Run the configured check command inside the overlay.
    Check,
    /// End the turn.
    Finish { summary: String },
}

impl Action {
    pub fn tool_name(&self) -> &'static str {
        match self {
            Action::Read { .. } => "read",
            Action::Search { .. } => "search",
            Action::Edit { .. } => "edit",
            Action::Check => "check",
            Action::Finish { .. } => "finish",
        }
    }
}

/// What the model gets back. Every variant is truncated to a per-tool cap before
/// it enters the prompt; at 13 tok/s an untruncated observation is a minute of
/// wall-clock the user paid for and did not want.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Observation {
    FileSlice { path: PathBuf, first_line: u32, text: String, truncated: bool },
    SearchHits { hits: Vec<SymbolHit>, truncated: bool },
    /// The gate's verdict, rendered for the model.
    GateVerdict { accepted: bool, detail: String },
    CheckOutput { clean: bool, diagnostics: String, truncated: bool },
    /// A deterministic refusal produced without calling the model or the tool:
    /// malformed argument, unread path, oversized edit. Costs zero tokens to
    /// produce and is the cheapest correction in the system.
    Refused { reason: BounceReason, detail: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolHit {
    pub symbol: String,
    pub signature: String,
    pub path: PathBuf,
    pub line: u32,
}

// ---------------------------------------------------------------------------
// §6.2 / §7 Gate outcomes.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BounceReason {
    /// `search` matched nothing on disk. The model is inventing file contents;
    /// this is the observed dominant failure of an unindexed cold turn.
    SearchNotFound,
    /// `search` matched more than once. Ambiguous edits are never applied.
    SearchAmbiguous,
    /// The path was never read in this session. Refused before any file I/O.
    PathNotRead,
    /// Replacement exceeded `gate.max_edit_lines`.
    EditTooLarge,
    /// The overlay failed `cargo check`.
    CheckFailed,
    /// The file changed underneath us between proposal and approval.
    StaleOnApply,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EscalationTrigger {
    /// `gate.max_repairs` bounces on one edit.
    RepairsExhausted,
    /// The same rustc error code twice across different repair attempts, which
    /// means the model is circling rather than converging.
    ErrorCodeRepeated,
    UserRequested,
}
