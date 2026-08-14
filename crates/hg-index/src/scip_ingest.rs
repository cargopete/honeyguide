//! SCIP ingest: the structural half of `.agent-index/` (RFC-0001 §5.1 pass 1).
//!
//! This module exists to answer exactly one question correctly:
//!
//! > every reference to *this* symbol, and nowhere else.
//!
//! That sounds like something a search can approximate. It is not. RFC-0003
//! §3.2a records the measurement: propagating a rename of `Catalogue::count`
//! using a whole-token search rewrote 84 sites across 15 files on the M0 target,
//! and not one of them referenced the renamed method. They were
//! `Iterator::count()`, struct fields named `count`, and local bindings named
//! `count`, in crates that do not depend on the one being edited.
//!
//! A token search cannot distinguish a method on one type from an identically
//! named method on another, and the identifiers people actually rename are the
//! common ones. So mechanical propagation waits for this module, and gets its
//! reference set from `rust-analyzer scip`, which resolves types and generics
//! and is right rather than nearly right.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use protobuf::Message;

/// One occurrence of a symbol in the source, as SCIP records it.
///
/// Line and column are zero-based, matching SCIP; callers converting to a
/// human-facing location add one to the line and nothing to the column.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Occurrence {
    pub path: PathBuf,
    pub line: u32,
    pub col_start: u32,
    pub col_end: u32,
    /// True when this occurrence *is* the definition rather than a use of it.
    pub is_definition: bool,
}

/// A parsed `index.scip`, arranged for the two lookups the harness performs:
/// "what symbol is defined here" and "where else is that symbol used".
#[derive(Debug, Default)]
pub struct ScipIndex {
    by_symbol: HashMap<String, Vec<Occurrence>>,
    /// Short display name -> the SCIP symbol ids that carry it. One short name
    /// can map to many symbols, which is the whole reason the lexical version
    /// could not work.
    by_name: HashMap<String, Vec<String>>,
}

#[derive(Debug)]
pub enum IngestError {
    Io(std::io::Error),
    Decode(protobuf::Error),
}

impl std::fmt::Display for IngestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(e) => write!(f, "reading the SCIP index: {e}"),
            Self::Decode(e) => write!(f, "decoding the SCIP index: {e}"),
        }
    }
}

impl std::error::Error for IngestError {}

impl From<std::io::Error> for IngestError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

impl From<protobuf::Error> for IngestError {
    fn from(e: protobuf::Error) -> Self {
        Self::Decode(e)
    }
}

/// SCIP's `SymbolRole::Definition` is bit 0 of a bitfield, not an enum value.
const ROLE_DEFINITION: i32 = 1;

impl ScipIndex {
    /// Parse an `index.scip` produced by `rust-analyzer scip <dir>`.
    pub fn load(path: impl AsRef<Path>) -> Result<Self, IngestError> {
        let bytes = std::fs::read(path)?;
        let index = scip::types::Index::parse_from_bytes(&bytes)?;

        let mut out = Self::default();
        for doc in &index.documents {
            let file = PathBuf::from(&doc.relative_path);
            for occ in &doc.occurrences {
                if occ.symbol.is_empty() {
                    continue;
                }
                // SCIP packs ranges as [line, colStart, colEnd] for the single
                // line case and [startLine, startCol, endLine, endCol]
                // otherwise. Anything else is malformed and is skipped rather
                // than guessed at.
                let (line, col_start, col_end) = match occ.range.as_slice() {
                    [l, cs, ce] => (*l, *cs, *ce),
                    [l, cs, _le, ce] => (*l, *cs, *ce),
                    _ => continue,
                };
                out.by_symbol
                    .entry(occ.symbol.clone())
                    .or_default()
                    .push(Occurrence {
                        path: file.clone(),
                        line: line.max(0) as u32,
                        col_start: col_start.max(0) as u32,
                        col_end: col_end.max(0) as u32,
                        is_definition: occ.symbol_roles & ROLE_DEFINITION != 0,
                    });
            }
        }

        for symbol in out.by_symbol.keys() {
            if let Some(name) = short_name(symbol) {
                out.by_name.entry(name).or_default().push(symbol.clone());
            }
        }
        Ok(out)
    }

    /// Every occurrence of a symbol, definition included.
    pub fn occurrences(&self, symbol: &str) -> &[Occurrence] {
        self.by_symbol.get(symbol).map_or(&[], Vec::as_slice)
    }

    /// Every occurrence that is *not* the definition: the sites a rename must
    /// also update.
    pub fn references(&self, symbol: &str) -> impl Iterator<Item = &Occurrence> {
        self.occurrences(symbol).iter().filter(|o| !o.is_definition)
    }

    /// The symbol whose definition covers this file, line and column.
    ///
    /// This is the entry point for RFC-0003 §3: the model renames a definition,
    /// the harness asks what was defined there, and only then asks where else it
    /// is used. Going straight from a name to a reference set is the mistake
    /// §3.2a measured.
    ///
    /// The column is required, and that is not fussiness. `pub fn count(&self,
    /// query: &str)` defines both the method and the parameter `query` on one
    /// line, so a line-only lookup has to choose between them — and choosing by
    /// walking a `HashMap` means choosing at random, which is what the first
    /// version of this did. Probed against a real index it returned `local 25`,
    /// the parameter, in place of the method.
    pub fn defined_at(&self, path: &Path, line: u32, col: u32) -> Option<&str> {
        self.by_symbol
            .iter()
            .filter(|(_, occs)| {
                occs.iter().any(|o| {
                    o.is_definition
                        && o.line == line
                        && o.path == path
                        && o.col_start <= col
                        && col < o.col_end
                })
            })
            // A tie is still possible in principle; prefer a global symbol over a
            // local, since a local is never the thing a repo-wide rename means.
            .min_by_key(|(symbol, _)| symbol.starts_with("local ") as u8)
            .map(|(symbol, _)| symbol.as_str())
    }

    /// Symbols sharing a short display name. Useful for diagnostics and for
    /// showing a user why a rename is ambiguous; never as a reference set.
    pub fn symbols_named(&self, name: &str) -> &[String] {
        self.by_name.get(name).map_or(&[], Vec::as_slice)
    }

    pub fn symbol_count(&self) -> usize {
        self.by_symbol.len()
    }

    pub fn occurrence_count(&self) -> usize {
        self.by_symbol.values().map(Vec::len).sum()
    }
}

/// Pull the trailing identifier out of a SCIP symbol string.
///
/// A rust-analyzer symbol looks like
/// `rust-analyzer cargo dipper-index 0.1.0 wire/Bitfield#count.`, and the part
/// worth showing a human is `count`. The descriptor suffixes SCIP uses are
/// documented in its schema: `#` for a type, `.` for a term, `()` for a method,
/// `/` for a namespace.
///
/// Inherent methods take a different shape that cost this parser a bug: an
/// `impl` block is written `impl#[Catalogue]count().`, with the receiver in
/// brackets. Measured against a real index rather than assumed, which is how the
/// bracketed form turned up at all.
fn short_name(symbol: &str) -> Option<String> {
    let tail = symbol.rsplit(' ').next()?;
    let last = tail
        .rsplit(|c| c == '#' || c == '/' || c == ':' || c == ']')
        .find(|s| !s.is_empty())?;
    let name: String = last
        .trim_end_matches('.')
        .trim_end_matches("()")
        .trim_end_matches('.')
        .to_string();
    (!name.is_empty() && name.chars().all(|c| c.is_alphanumeric() || c == '_')).then_some(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_name_of_a_method() {
        assert_eq!(
            short_name("rust-analyzer cargo dipper-index 0.1.0 Catalogue#count()."),
            Some("count".into())
        );
    }

    /// The form an inherent method actually takes, taken from a real index.
    #[test]
    fn short_name_of_an_inherent_method() {
        assert_eq!(
            short_name("rust-analyzer cargo dipper-index 0.1.0 impl#[Catalogue]count()."),
            Some("count".into())
        );
    }

    #[test]
    fn short_name_of_a_module_scoped_field() {
        assert_eq!(
            short_name("rust-analyzer cargo dipper-bt 0.1.0 wire/Bitfield#count."),
            Some("count".into())
        );
    }

    #[test]
    fn short_name_of_a_type() {
        assert_eq!(
            short_name("rust-analyzer cargo dipper-index 0.1.0 Record#"),
            Some("Record".into())
        );
    }

    #[test]
    fn short_name_rejects_punctuation_only() {
        assert_eq!(short_name("rust-analyzer cargo x 0.1.0 "), None);
    }

    #[test]
    fn empty_index_answers_nothing_rather_than_panicking() {
        let idx = ScipIndex::default();
        assert!(idx.occurrences("anything").is_empty());
        assert_eq!(idx.references("anything").count(), 0);
        assert_eq!(idx.defined_at(Path::new("a.rs"), 1, 0), None);
    }
}
