//! Probe a real `index.scip`, to check the ingest against something rust-analyzer
//! actually produced rather than against my idea of what it produces.
//!
//! Its reason for existing is RFC-0003 §3.2a: a lexical rename of
//! `Catalogue::count` rewrote 84 sites across 15 files on this exact repository.
//! This prints what the reference set really is.
//!
//! ```text
//! rust-analyzer scip /path/to/dipper --output /tmp/dipper.scip
//! cargo run -p hg-index --example scip_probe -- /tmp/dipper.scip \
//!     crates/dipper-index/src/lib.rs 161 11 count
//! ```

use std::path::Path;

use hg_index::scip_ingest::ScipIndex;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let scip = args.next().expect("usage: scip_probe <index.scip> <file> <line0> <col0> <name>");
    let file = args.next().expect("missing <file>");
    let line: u32 = args.next().expect("missing <line0>").parse()?;
    let col: u32 = args.next().expect("missing <col0>").parse()?;
    let name = args.next().expect("missing <name>");

    let idx = ScipIndex::load(&scip)?;
    println!(
        "loaded {}: {} symbols, {} occurrences",
        scip,
        idx.symbol_count(),
        idx.occurrence_count()
    );

    // The lexical trap, quantified: how many distinct symbols carry this name?
    let sharing = idx.symbols_named(&name);
    println!("\n{} distinct symbols are named `{name}`:", sharing.len());
    for s in sharing.iter().take(12) {
        println!("   {s}");
    }
    if sharing.len() > 12 {
        println!("   ... and {} more", sharing.len() - 12);
    }

    let Some(symbol) = idx.defined_at(Path::new(&file), line, col) else {
        println!("\nnothing is defined at {file}:{line}:{col} (0-based)");
        return Ok(());
    };
    println!("\ndefined at {file}:{line}:{col} ->\n   {symbol}");

    let refs: Vec<_> = idx.references(symbol).collect();
    println!("\n{} reference(s) to THAT symbol:", refs.len());
    for r in &refs {
        println!("   {}:{}:{}", r.path.display(), r.line + 1, r.col_start);
    }
    Ok(())
}
