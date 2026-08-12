//! `.agent-index/` generation, refresh, and structural retrieval.
//!
//! RFC-0001 §5. This is where the asymmetry lives: a strong model writes here,
//! offline and occasionally; the local model only ever reads. Types only for
//! now; behaviour lands at M1.

#![forbid(unsafe_code)]

use std::path::PathBuf;

/// `.agent-index/manifest.toml`.
///
/// Every artifact records the git rev it was generated at, because a map that
/// does not know how stale it is will be trusted exactly when it should not be.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub schema_version: u32,
    pub artifacts: Vec<ArtifactRecord>,
    /// Absent when the index was built in degraded mode (RFC-0001 §5.1 pass 3).
    pub semantic_model: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactRecord {
    pub name: String,
    pub git_rev: String,
    pub generated_at: String,
    pub bytes: u64,
}

/// A symbol as rust-analyzer's SCIP output sees it: type-aware, generic-resolved,
/// and therefore worth considerably more than a tree-sitter guess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    pub id: String,
    pub kind: SymbolKind,
    pub signature: String,
    pub path: PathBuf,
    pub line: u32,
    pub module: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SymbolKind {
    Function,
    Method,
    Struct,
    Enum,
    Trait,
    TraitImpl,
    Const,
    TypeAlias,
    Module,
}

/// One module summary, written by the strong model. Two to five sentences:
/// responsibility, key types, invariants, gotchas. These let the local model
/// know what a file does without paying to read it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModuleSummary {
    pub module: String,
    pub path: PathBuf,
    pub text: String,
    pub git_rev: String,
}

/// What retrieval assembles before the first model turn of a task (§5.3).
///
/// Assembled by the harness, not requested by the model. The model does not
/// explore; that is the entire point.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskContext {
    pub repo_map: String,
    pub project_brief: String,
    pub summaries: Vec<ModuleSummary>,
    pub signatures: Vec<Symbol>,
    /// Symbols reachable within one hop in the call graph. Bodies are fetched
    /// from disk for these and only these.
    pub blast_radius: Vec<Symbol>,
    pub stale: Option<StalenessWarning>,
}

/// Emitted when the index predates HEAD *and* the drift touches this task.
/// A stale index is still a useful map; it is never ground truth for file
/// contents, which always come from disk at edit time.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StalenessWarning {
    pub index_rev: String,
    pub head_rev: String,
    pub changed_files_in_radius: Vec<PathBuf>,
}
