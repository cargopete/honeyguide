//! Backend abstraction over the local model server, plus schema management for
//! constrained action emission.
//!
//! RFC-0001 §8. Types only for now; behaviour lands at M2.

#![forbid(unsafe_code)]

/// Which serving stack we are talking to.
///
/// v0.1 targets Ollama, because that is what is actually running on the
/// ThinkPad. The distinction is not cosmetic: Ollama gives us JSON-schema
/// constraint (`format`) but no GBNF, no chat-template control, and no explicit
/// slot management, so several RFC decisions differ per backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    Ollama,
    /// Planned. Buys GBNF and `--jinja`, at the cost of running our own server.
    LlamaServer,
}

/// The two generation phases of one ReAct turn (RFC-0001 §6).
///
/// Reasoning is unconstrained so the model can think in its trained format;
/// only the action block is constrained. Constraining the thinking is what
/// costs 10-30% on reasoning benchmarks, and it buys nothing here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Reason,
    Action,
}

/// One completion request.
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub phase: Phase,
    /// Rendered messages. Ordering is append-only across a turn so the server's
    /// prefix cache can hit; see `Usage::prefill_cached`.
    pub messages: Vec<Message>,
    /// JSON schema for the action object. `None` during `Phase::Reason`.
    pub schema: Option<String>,
    pub stops: Vec<String>,
    pub num_predict: u32,
    pub num_ctx: u32,
    pub sampling: Sampling,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub role: Role,
    pub content: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sampling {
    pub temperature: f32,
    pub top_p: f32,
    pub top_k: u32,
    pub min_p: f32,
}

impl Default for Sampling {
    /// Deliberately explicit. The GGUF advertises temp 1.0 / top_k 20 / top_p
    /// 0.95 and the Ollama Modelfile carries no PARAMETER lines, so anything we
    /// do not send is inherited from whatever Ollama defaults to that week.
    fn default() -> Self {
        Self { temperature: 0.3, top_p: 0.95, top_k: 20, min_p: 0.05 }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Completion {
    pub text: String,
    pub usage: Usage,
    /// True when generation stopped at `num_predict` rather than at a stop
    /// token or end of a satisfied schema. A truncated action is discarded, not
    /// repaired: half a JSON object tells us nothing.
    pub hit_token_cap: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Usage {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    /// Time actually spent ingesting the prompt. On this hardware prefill is
    /// the binding cost, so this is the single most important number in the
    /// telemetry.
    pub prefill_ms: u64,
    pub decode_ms: u64,
}

impl Usage {
    /// Whether the KV prefix cache served this prompt.
    ///
    /// Ollama reports `prompt_eval_count` as the number of tokens the prompt
    /// contains, not the number it recomputed, so a cache hit is invisible in
    /// the token counts and shows up only as a collapsed duration. Measured on
    /// the reference machine: 5,136 tokens took 77.2s cold and 0.2s warm, so
    /// the two populations are three orders of magnitude apart and any
    /// threshold in between will do.
    ///
    /// A backend that reports reuse honestly should override this.
    pub fn prefix_cache_hit(&self) -> bool {
        self.prompt_tokens > 0 && self.effective_prefill_tok_s() > 1_000.0
    }

    pub fn effective_prefill_tok_s(&self) -> f32 {
        if self.prefill_ms == 0 {
            return f32::INFINITY;
        }
        self.prompt_tokens as f32 / (self.prefill_ms as f32 / 1000.0)
    }
}

/// Result of the mandatory startup probe (RFC-0001 §8.4).
///
/// `hg` refuses to start on a failed probe rather than degrading silently. A
/// context window quietly truncated to 4096 presents as "the model cannot use
/// tools", and that misdiagnosis has cost the wider community entire weekends.
#[derive(Debug, Clone, PartialEq)]
pub struct ServingProbe {
    pub backend: Backend,
    pub model: String,
    pub architecture: String,
    pub quantization: String,
    pub advertised_ctx: u32,
    pub honoured_ctx: u32,
    pub supports_schema_constraint: bool,
    pub measured_decode_tok_s: f32,
    pub measured_prefill_tok_s: f32,
}
