# Project brief: dipper

Hand-written stand-in for `.agent-index/` (AGENTS.md plus the module summary and
symbol signatures that `hg index` will generate). Kept deliberately short: this
is roughly the budget RFC-0001 §8.5 allows for a project brief plus one module.

## What it is

`dipper` is a single-binary Rust BitTorrent client: magnet link, `.torrent` file
or archive.org identifier in, verified bytes out. Cargo workspace, edition 2024.

Build and test:

```
cargo check --workspace
cargo test -p <crate>
```

## Crates

| Crate | Responsibility |
|---|---|
| `dipper-bt` | BitTorrent protocol: peer wire, trackers, DHT, piece picker |
| `dipper-ia` | archive.org client: metadata, search, S3 |
| `dipper-index` | Local full-text catalogue of archive.org items, over tantivy |
| `dipper-web` | HTTP interface, streaming and transcoding |
| `dipper-cli` | Command-line entry point |

## Module: `crates/dipper-index/src/lib.rs`

The whole crate is one 413-line file. It wraps a tantivy index of archive.org
items behind a small typed API, so callers never touch tantivy directly.
Everything returns `Result<T, Error>` where `Error` wraps tantivy and IO
failures. Read and write are deliberately separate types: `Catalogue` reads,
`Harvest` writes, and a `Harvest` must be committed for its writes to become
visible to searches.

Key types and signatures:

```rust
pub struct Record {          // one indexed archive.org item
    pub identifier: String,
    pub title: Option<String>,
    pub creator: Option<String>,
    pub description: Option<String>,
    pub mediatype: Option<String>,
    pub publicdate: Option<String>,
    pub subjects: Vec<String>,
    pub collections: Vec<String>,
    pub downloads: u64,
    pub item_size: u64,
    pub has_torrent: bool,
}                            // derives Debug, Clone, Default, PartialEq, Eq

pub struct Hit { pub record: Record, pub score: f32 }

pub struct Catalogue { /* reader + schema fields */ }
impl Catalogue {
    pub fn open(dir: impl AsRef<Path>) -> Result<Self>
    pub fn in_memory() -> Result<Self>
    pub fn writer(&self) -> Result<Harvest>
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<Hit>>
    pub fn count(&self, query: &str) -> Result<usize>
    pub fn len(&self) -> Result<usize>
    pub fn is_empty(&self) -> Result<bool>
    pub fn get(&self, identifier: &str) -> Result<Option<Record>>
}

pub struct Harvest { /* tantivy IndexWriter */ }
impl Harvest {
    pub fn upsert(&self, record: &Record) -> Result<()>
    pub fn commit(&mut self) -> Result<()>
    pub fn clear(&mut self) -> Result<()>
}
```

Gotchas:

- `len` and `is_empty` call `reader.reload()` first; stale readers are the usual
  cause of a "missing" document that was definitely written.
- The unit tests live in a `mod tests` at the bottom of the same file, so a
  change to a public method's name has to be fixed there too.
