//! Context budget hook: one checkpoint reminder per crossing, never a denial (forced handoffs
//! break the flow). Reads only the transcript tail and fails open on any error. On PreCompact it
//! also tells the summary what must survive.
use serde_json::{Value, json};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

#[path = "../state/ledger.rs"]
mod ledger;

const TAIL_BYTES: u64 = 256 * 1024;
// Early enough to checkpoint the task file before auto-compaction (observed near 217k).
const DEFAULT_SOFT: u64 = 150_000;
// Observed auto-compaction point: a reminder configured above it never fires (a "1M" setting on
// a window that still compacts near 217k), so the limit is capped at this share of it.
const COMPACT_MARK: &[u8] = b"\"isCompactSummary\":true";
const FLOOR_SOFT_PERCENT: u64 = 69;
const FLOOR_MIN_TOKENS: u64 = 50_000;
const FLOOR_MAX_AGE: std::time::Duration = std::time::Duration::from_secs(14 * 24 * 3600);
const COMPACTED_NOTE: &str = "Context was compacted: earlier details are lossy. Run `omega resume [TASK]`, then re-read only what the next action needs.";
const COMPACT_INSTRUCTIONS: &str = "Keep in the summary: the user's requests and corrections in their own words; the path of the active task file under ~/.agents/tasks/active/ and its next action; decisions and facts verified in this session; files changed and not yet committed; evidence paths and validation commands with their exit status; approaches that failed, so they are not retried. Leave out tool output bodies and file contents: they can be read again.";

fn limit(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

fn floor_file(home: &Path) -> Option<std::path::PathBuf> {
    ledger::state_file(home, "budget", "compact-floor")
}

/// Context size at the latest automatic compaction, if observed within FLOOR_MAX_AGE.
fn compact_floor(home: &Path) -> Option<u64> {
    let path = floor_file(home)?;
    if path.metadata().ok()?.modified().ok()?.elapsed().ok()? > FLOOR_MAX_AGE {
        return None;
    }
    let value = serde_json::from_slice::<Value>(&ledger::read_bounded(&path)?).ok()?;
    value["tokens"].as_u64().filter(|t| *t >= FLOOR_MIN_TOKENS)
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
        // Older usage predates the compaction: the transcript lags behind the first tool calls.
        if line.windows(COMPACT_MARK.len()).any(|w| w == COMPACT_MARK)
            && serde_json::from_slice::<Value>(line).is_ok_and(|e| e["isCompactSummary"] == true)
        {
            return None;
        }
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

fn evaluate(payload: &Value) -> Value {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    let state = home
        .as_deref()
        .zip(payload.get("session_id").and_then(Value::as_str))
        .and_then(|(h, s)| ledger::state_file(h, "budget", s));
    let event = payload.get("hook_event_name").and_then(Value::as_str).unwrap_or("");
    let tokens = || payload.get("transcript_path").and_then(Value::as_str).and_then(|p| context_tokens(Path::new(p)));
    if event == "PreCompact" {
        // The newest usage is the context that triggered compaction: calibrate later sessions.
        if payload["trigger"] == "auto" && payload.get("agent_id").is_none() {
            if let Some((tokens, path)) = tokens().filter(|t| *t >= FLOOR_MIN_TOKENS).zip(home.as_deref().and_then(floor_file)) {
                let _ = ledger::write_private(&path, json!({"tokens": tokens}).to_string().as_bytes());
            }
        }
        // Claude Code uses PreCompact stdout as instructions for the summary. Subagents have no
        // task file; instructions the user typed after /compact are left alone.
        let custom = payload.get("custom_instructions").and_then(Value::as_str).is_some_and(|c| !c.trim().is_empty());
        return if payload.get("agent_id").is_some() || custom { Value::Null } else { Value::from(COMPACT_INSTRUCTIONS) };
    }
    let is_reset = event == "PostCompact"
        || (event == "SessionStart" && matches!(payload.get("source").and_then(Value::as_str), Some("compact" | "clear")));
    if is_reset {
        if let Some(state) = &state {
            ledger::remove(state);
        }
        return if event == "SessionStart" && payload["source"] == "compact" {
            json!({"hookSpecificOutput": {"hookEventName": event, "additionalContext": COMPACTED_NOTE}})
        } else {
            json!({})
        };
    }
    // PreToolUse is ignored: settings written by earlier versions may still register it.
    if payload.get("agent_id").is_some() || !matches!(event, "PostToolUse" | "UserPromptSubmit") {
        return json!({});
    }
    let (Some(tokens), Some(state)) = (tokens(), state) else { return json!({}) };
    let soft = limit("OMEGA_CONTEXT_SOFT", DEFAULT_SOFT);
    let soft = home.as_deref().and_then(compact_floor).map_or(soft, |f| soft.min(f * FLOOR_SOFT_PERCENT / 100));
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
            "Context {}k: update ~/.agents/tasks/active/<task>.md now (verified facts, state, next action), then keep working.",
            tokens / 1000
        ),
    }})
}

fn main() {
    let mut input = Vec::new();
    let output = match std::io::stdin().read_to_end(&mut input) {
        Ok(_) => serde_json::from_slice::<Value>(&input).map_or_else(|_| json!({}), |p| evaluate(&p)),
        Err(_) => json!({}),
    };
    // PreCompact: plain text is the instruction, nothing means no instruction.
    let _ = match &output {
        Value::Null => Ok(()),
        Value::String(text) => writeln!(std::io::stdout().lock(), "{text}"),
        other => writeln!(std::io::stdout().lock(), "{other}"),
    };
}
