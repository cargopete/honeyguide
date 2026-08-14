//! `hg-scip-refs <index.scip> <file> <line0> <col0>`
//!
//! Resolves the symbol defined at a position and prints its reference sites as
//! JSON. This is the reference set RFC-0003 §3 needs, and the whole reason it
//! exists as a separate query rather than a name lookup: asked for "references
//! to the name `count`" the honest answer on the M0 target is 84 sites in 15
//! files, of which 3 are correct. Asked for "references to *this* symbol" it is
//! 3, and they are the right 3.
//!
//! JSON is hand-written rather than pulled in via serde. The output is four
//! scalars per row and a dependency is not worth the convenience.

use std::path::Path;

use hg_index::scip_ingest::ScipIndex;

fn esc(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [scip, file, line, col] = args.as_slice() else {
        eprintln!("usage: hg-scip-refs <index.scip> <file> <line0> <col0>");
        std::process::exit(2);
    };
    let (line, col): (u32, u32) = (line.parse()?, col.parse()?);

    let idx = ScipIndex::load(scip)?;
    let Some(symbol) = idx.defined_at(Path::new(file), line, col) else {
        // Not an error: the caller asked whether a symbol is defined here, and
        // the answer is no. An empty reference set means "propagate nothing".
        println!(r#"{{"symbol":null,"refs":[]}}"#);
        return Ok(());
    };

    let refs: Vec<String> = idx
        .references(symbol)
        .map(|r| {
            format!(
                r#"{{"path":"{}","line":{},"col_start":{},"col_end":{}}}"#,
                esc(&r.path.to_string_lossy()),
                r.line,
                r.col_start,
                r.col_end
            )
        })
        .collect();

    println!(
        r#"{{"symbol":"{}","refs":[{}]}}"#,
        esc(symbol),
        refs.join(",")
    );
    Ok(())
}
