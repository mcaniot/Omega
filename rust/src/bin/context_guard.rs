//! Bound Claude Read calls without changing media reads. The only `allow` is the bounded rewrite
//! of an oversized Read; Claude Code still evaluates deny and ask rules after a hook `allow`.
//! Also keeps a per-session ledger of line ranges already in context to deny re-reads.
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

#[path = "../state/ledger.rs"]
mod ledger;
use ledger::{Coverage, Ledger};

const LARGE_TEXT_BYTES: u64 = 8192;
const MAX_READ_LINES: u64 = 200;
const MAX_COUNTED_BYTES: u64 = 1 << 20;
const CPP_HINT: &str = "C/C++ edited: if this code can run in a realtime or control path, load the `cpp-quality` skill and re-check the change (no lock, heap allocation, blocking call or unbounded work).";

fn deny(reason: &str) -> Value {
    json!({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})
}

fn session_ledger(payload: &Value) -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    ledger::state_file(Path::new(&home), "read-ledger", payload.get("session_id")?.as_str()?)
}

/// Lines in the file, when it is small enough to count cheaply.
fn line_count(file: &str, metadata: &std::fs::Metadata) -> Option<u64> {
    (metadata.len() <= MAX_COUNTED_BYTES)
        .then(|| std::fs::read(file).ok())
        .flatten()
        .map(|b| b.iter().filter(|&&c| c == b'\n').count() as u64 + u64::from(b.last().is_some_and(|&c| c != b'\n')))
}

/// Deny re-reads of unchanged lines already in context; record the range otherwise.
fn ledger_check(payload: &Value, file: &str, metadata: &std::fs::Metadata) -> Option<Value> {
    if payload.get("agent_id").is_some() {
        return None; // Subagents have their own context.
    }
    let state = session_ledger(payload)?;
    let input = &payload["tool_input"];
    let start = match input.get("offset") {
        None | Some(Value::Null) => 1,
        Some(v) => v.as_u64().filter(|&n| n >= 1)?,
    };
    let limit = match input.get("limit") {
        None | Some(Value::Null) => None,
        Some(v) => Some(v.as_u64().filter(|&n| n >= 1)?),
    };
    let total = line_count(file, metadata);
    let end = match (limit, total) {
        (Some(n), Some(t)) => (start + n - 1).min(t),
        (Some(n), None) => start + n - 1,
        (None, Some(t)) => t,
        (None, None) => return None,
    };
    if end < start {
        return None;
    }
    let key = ledger::key(Path::new(file));
    let stamp = ledger::stamp(metadata);
    let mut book = Ledger::load(&state);
    match book.check(&key, stamp, start, end) {
        Coverage::Full => Some(deny(&format!(
            "already in context: lines {start}-{end} of {file} (unchanged since read)"
        ))),
        Coverage::Partial(a, b) => Some(deny(&format!(
            "partly in context (unchanged since read): Read {file} with offset={a} limit={} for lines {a}-{b} only",
            b - a + 1
        ))),
        Coverage::Missing => {
            book.record(&key, stamp, start, end);
            book.save(&state);
            None
        }
    }
}

/// Drop ledger state on context reset or file mutation.
fn invalidate(payload: &Value) {
    let Some(state) = session_ledger(payload) else { return };
    let event = payload.get("hook_event_name").and_then(Value::as_str);
    let reset = event == Some("PostCompact")
        || (event == Some("SessionStart")
            && matches!(payload.get("source").and_then(Value::as_str), Some("compact" | "clear")));
    if reset {
        ledger::remove(&state);
    } else if event == Some("PostToolUse")
        && matches!(payload.get("tool_name").and_then(Value::as_str), Some("Edit" | "Write" | "MultiEdit"))
    {
        if let Some(file) = payload["tool_input"].get("file_path").and_then(Value::as_str) {
            let mut book = Ledger::load(&state);
            book.forget(&ledger::key(Path::new(file)));
            book.save(&state);
        }
    }
}

/// Once per session or subagent, after the first C/C++ edit: point at the realtime checklist.
/// Measured: the model never loads `cpp-quality` on its own, and it is the one skill that
/// changed correctness (mutex and allocation in a control step).
fn cpp_hint(payload: &Value) -> Option<Value> {
    if payload.get("hook_event_name").and_then(Value::as_str) != Some("PostToolUse") {
        return None;
    }
    let file = payload["tool_input"].get("file_path").and_then(Value::as_str)?;
    let extension = Path::new(file).extension()?.to_str()?.to_ascii_lowercase();
    if !matches!(extension.as_str(), "c" | "cc" | "cpp" | "cxx" | "h" | "hh" | "hpp" | "hxx" | "ipp") {
        return None;
    }
    let owner = payload.get("agent_id").or_else(|| payload.get("session_id"))?.as_str()?;
    let state = ledger::state_file(Path::new(&std::env::var_os("HOME")?), "cpp-hint", owner)?;
    if state.exists() {
        return None;
    }
    ledger::write_private(&state, b"{}").ok()?;
    Some(json!({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": CPP_HINT,
    }}))
}

fn evaluate(payload: &Value) -> Value {
    let event = payload.get("hook_event_name").and_then(Value::as_str).unwrap_or("PreToolUse");
    if event != "PreToolUse" {
        invalidate(payload);
        return cpp_hint(payload).unwrap_or_else(|| json!({}));
    }
    if payload.get("tool_name").and_then(Value::as_str) != Some("Read") {
        return json!({});
    }
    let input = &payload["tool_input"];
    let Some(file) = input.get("file_path").and_then(Value::as_str) else {
        return json!({}); // Let Read report its own argument errors.
    };
    let path = Path::new(file);
    let Ok(metadata) = path.metadata() else {
        return json!({}); // Preserve native access checks and missing-file errors.
    };
    if !metadata.is_file() {
        return json!({});
    }
    let extension = path
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if matches!(
        extension.as_str(),
        "png"
            | "jpg"
            | "jpeg"
            | "gif"
            | "webp"
            | "bmp"
            | "tif"
            | "tiff"
            | "ico"
            | "avif"
            | "pdf"
            | "ipynb"
    ) {
        return json!({}); // Serialized image bytes are not text tokens.
    }
    let tracked = || ledger_check(payload, file, &metadata).unwrap_or_else(|| json!({}));
    if metadata.len() <= LARGE_TEXT_BYTES {
        return tracked();
    }
    if let (Some(home), Ok(real)) = (std::env::var_os("HOME"), path.canonicalize()) {
        let evidence = Path::new(&home).join(".agents/tasks/evidence");
        let evidence = evidence.canonicalize().unwrap_or(evidence);
        if real.starts_with(evidence) {
            return deny(
                "Large evidence file: use Ω-inspect ARTIFACT with --search or --offset/--lines; do not Read the full log.",
            );
        }
    }
    match input.get("limit").and_then(Value::as_u64) {
        Some(1..=MAX_READ_LINES) => tracked(),
        _ => bounded(payload, file, &metadata),
    }
}

/// Run an unbounded or oversized Read of a large text file as its first MAX_READ_LINES lines
/// instead of denying it: a denial costs one full-context round trip for the same result.
fn bounded(payload: &Value, file: &str, metadata: &std::fs::Metadata) -> Value {
    let mut payload = payload.clone();
    payload["tool_input"]["limit"] = json!(MAX_READ_LINES);
    if let Some(denied) = ledger_check(&payload, file, metadata) {
        return denied;
    }
    let start = payload["tool_input"].get("offset").and_then(Value::as_u64).unwrap_or(1).max(1);
    let end = start + MAX_READ_LINES - 1;
    let range = match line_count(file, metadata) {
        Some(total) => format!("{start}-{} of {total}", end.min(total)),
        None => format!("{start}-{end}"),
    };
    json!({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "Omega guard: large text file, Read bounded to 200 lines.",
        "updatedInput": payload["tool_input"],
        "additionalContext": format!(
            "Omega guard bounded this Read to lines {range} of {file}. Locate the symbol with rg, then Read with offset and limit ≤200; do not page through the file."
        ),
    }})
}

fn main() {
    let mut input = Vec::new();
    let output = match std::io::stdin().read_to_end(&mut input) {
        Ok(_) => match serde_json::from_slice::<Value>(&input) {
            Ok(payload) => evaluate(&payload),
            Err(_) => deny("Invalid context guard JSON."),
        },
        Err(_) => deny("Cannot read context guard input."),
    };
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{output}");
}
