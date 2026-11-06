//! Context budget hook: one soft checkpoint reminder, then a hard stop that only allows
//! checkpointing. Reads only the transcript tail and fails open on any error.
use serde_json::{Value, json};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Component, Path};

#[path = "../state/ledger.rs"]
mod ledger;

const TAIL_BYTES: u64 = 256 * 1024;
// Last-resort guard just below observed auto-compaction (~200k): no handoff in normal sessions,
// no compaction (context rot) ever. With a 1M-context model raise via OMEGA_CONTEXT_SOFT/HARD.
const DEFAULT_SOFT: u64 = 150_000;
const DEFAULT_HARD: u64 = 180_000;
const HARD_REASON: &str = "Context budget reached: auto handoff. 1) Checkpoint ~/.agents/tasks/active/<task>.md (verified facts with file:line, current state, next action). 2) Run `'Ω-run' -- orca agent launch --agent claude --prompt 'omega resume <task>' --json`. 3) Report the new agent handle and stop.";

fn limit(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

/// Context tokens of the newest main-thread assistant message in the transcript tail.
fn context_tokens(transcript: &Path) -> Option<u64> {
    let mut file = std::fs::File::open(transcript).ok()?;
    let len = file.metadata().ok()?.len();
    let start = len.saturating_sub(TAIL_BYTES);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut tail = Vec::new();
    file.take(TAIL_BYTES).read_to_end(&mut tail).ok()?;
    let mut lines = tail.split(|&b| b == b'\n');
    if start > 0 {
        lines.next(); // First line is probably cut.
    }
    let lines: Vec<&[u8]> = lines.collect();
    for line in lines.iter().rev() {
        if !line.windows(7).any(|w| w == b"\"usage\"") {
            continue;
        }
        let Ok(entry) = serde_json::from_slice::<Value>(line) else { continue };
        if entry["type"] != "assistant" || entry["isSidechain"] == true {
            continue;
        }
        let usage = &entry["message"]["usage"];
        if usage.is_object() {
            return Some(
                ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"]
                    .iter()
                    .map(|k| usage[k].as_u64().unwrap_or(0))
                    .sum(),
            );
        }
    }
    None
}

fn under_tasks(file: &str, home: &Path) -> bool {
    let path = Path::new(file);
    if !path.is_absolute() || path.components().any(|c| c == Component::ParentDir) {
        return false;
    }
    let tasks = home.join(".agents/tasks");
    let tasks = tasks.canonicalize().unwrap_or(tasks);
    // Resolve symlinks through the nearest existing ancestor (the file may not exist yet).
    let mut existing = path;
    let mut tail = Vec::new();
    while !existing.exists() {
        let (Some(parent), Some(name)) = (existing.parent(), existing.file_name()) else { return false };
        tail.push(name);
        existing = parent;
    }
    let Ok(mut real) = existing.canonicalize() else { return false };
    real.extend(tail.iter().rev());
    real.starts_with(tasks)
}

/// `Ω-run ... -- omega ...` or `Ω-run --shell 'omega ...'`, without command chaining.
fn omega_command(command: &str) -> bool {
    if command.contains(['\n', ';', '&', '|', '<', '>', '$', '`', '(', ')']) {
        return false;
    }
    let tokens: Vec<&str> = command.split_whitespace().map(|t| t.trim_matches(['\'', '"'])).collect();
    let wrapper = tokens.first().is_some_and(|t| matches!(*t, "Ω-run" | "omega-run") || t.ends_with("/native/run"));
    let start = tokens.iter().position(|t| matches!(*t, "--" | "--shell")).map(|i| i + 1);
    let target = start.and_then(|i| tokens.get(i));
    // Auto handoff: a fresh agent in the current checkout resumes the task (`orca agent launch`).
    let handoff = start.is_some_and(|i| {
        matches!(tokens.get(i), Some(&("orca" | "orca-ide"))) && tokens.get(i + 1) == Some(&"agent") && tokens.get(i + 2) == Some(&"launch")
    });
    wrapper && (handoff || matches!(target, Some(&("omega" | "omega-optimize" | "Ω-optimize"))))
}

fn allowed(payload: &Value) -> bool {
    let input = &payload["tool_input"];
    let Some(home) = std::env::var_os("HOME") else { return false };
    match payload.get("tool_name").and_then(Value::as_str) {
        Some("Edit" | "Write" | "MultiEdit" | "Read") => input
            .get("file_path")
            .and_then(Value::as_str)
            .is_some_and(|f| under_tasks(f, Path::new(&home))),
        Some("Bash") => input.get("command").and_then(Value::as_str).is_some_and(omega_command),
        _ => false,
    }
}

fn evaluate(payload: &Value) -> Value {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    let state = home
        .as_deref()
        .zip(payload.get("session_id").and_then(Value::as_str))
        .and_then(|(h, s)| ledger::state_file(h, "budget", s));
    let event = payload.get("hook_event_name").and_then(Value::as_str).unwrap_or("");
    let is_reset = event == "PostCompact"
        || (event == "SessionStart" && matches!(payload.get("source").and_then(Value::as_str), Some("compact" | "clear")));
    if is_reset {
        if let Some(state) = &state {
            ledger::remove(state);
        }
        return json!({});
    }
    if payload.get("agent_id").is_some() || !matches!(event, "PreToolUse" | "PostToolUse" | "UserPromptSubmit") {
        return json!({});
    }
    let Some(tokens) = payload.get("transcript_path").and_then(Value::as_str).and_then(|p| context_tokens(Path::new(p))) else {
        return json!({});
    };
    let (soft, hard) = (limit("OMEGA_CONTEXT_SOFT", DEFAULT_SOFT), limit("OMEGA_CONTEXT_HARD", DEFAULT_HARD));
    if event == "PreToolUse" {
        return if tokens >= hard && !allowed(payload) {
            json!({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": HARD_REASON,
            }})
        } else {
            json!({})
        };
    }
    let Some(state) = state else { return json!({}) };
    let fired = ledger::read_bounded(&state)
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .is_some_and(|v| v["soft_fired"] == true);
    if tokens < soft {
        if fired {
            ledger::remove(&state); // Context shrank: re-arm the next crossing.
        }
        return json!({});
    }
    if fired {
        return json!({});
    }
    let _ = ledger::write_private(&state, b"{\"soft_fired\":true}");
    json!({"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": format!(
            "Context {}k/{}k: checkpoint ~/.agents/tasks/active/<task>.md now.",
            tokens / 1000,
            hard / 1000
        ),
    }})
}

fn main() {
    let mut input = Vec::new();
    let output = match std::io::stdin().read_to_end(&mut input) {
        Ok(_) => serde_json::from_slice::<Value>(&input).map_or_else(|_| json!({}), |p| evaluate(&p)),
        Err(_) => json!({}),
    };
    let _ = writeln!(std::io::stdout().lock(), "{output}");
}
