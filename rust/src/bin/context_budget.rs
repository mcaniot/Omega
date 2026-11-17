//! Context budget hook: one checkpoint reminder per crossing, never a forced handoff (it breaks the
//! flow). The only block is the cold-resume notice, once per idle period; resending the prompt
//! continues. Reads only the transcript tail and fails open on any error. On PreCompact it also
//! tells the summary what must survive.
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
// Prompt cache TTL of the main conversation on a Claude subscription (1 h) and the smallest context
// worth a restart: capsim replay on recorded sessions, 60k gave the best saving per restart.
const COLD_SECONDS: u64 = 3600;
const COLD_TOKENS: u64 = 60_000;
const TASK_SCAN_BYTES: u64 = 8 * 1024 * 1024;
const COMPACTED_NOTE: &str = "Context was compacted: earlier details are lossy. Run `'Ω-resume' [TASK]`, then re-read only what the next action needs.";
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

/// Seconds since the Unix epoch of an RFC 3339 UTC timestamp (`2026-11-16T14:30:06.807Z`).
fn epoch_seconds(stamp: &str) -> Option<i64> {
    let b = stamp.as_bytes();
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' || b[13] != b':' || b[16] != b':' || !stamp.ends_with('Z') {
        return None;
    }
    let num = |r: std::ops::Range<usize>| stamp.get(r)?.parse::<i64>().ok();
    let (y, m, d) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (hh, mm, ss) = (num(11..13)?, num(14..16)?, num(17..19)?);
    // Days from civil (Howard Hinnant), proleptic Gregorian.
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some((era * 146_097 + doe - 719_468) * 86_400 + hh * 3600 + mm * 60 + ss)
}

/// Context tokens of the newest main-thread assistant message in the transcript tail.
fn context_tokens(transcript: &Path) -> Option<u64> {
    newest_usage(transcript).map(|(tokens, _)| tokens)
}

/// Context tokens and timestamp of the newest main-thread assistant message in the transcript tail.
fn newest_usage(transcript: &Path) -> Option<(u64, Option<String>)> {
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
            let tokens = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"]
                .iter()
                .map(|k| usage[k].as_u64().unwrap_or(0))
                .sum();
            return Some((tokens, entry["timestamp"].as_str().map(str::to_owned)));
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
    if event == "UserPromptSubmit" {
        if let Some(block) = cold_resume(payload, home.as_deref()) {
            return block;
        }
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

/// Cold resume: after more than COLD_SECONDS idle the prompt cache (1 h TTL) has expired, so the next
/// request rewrites the whole context at the cache-write price (1 h write: 2x input; read: 0.05x on
/// Opus/Sonnet 5.5, 0.1x elsewhere). Recorded Opus sessions: restarting from the task file instead
/// saved 13-15 % of the cost (capsim replay). Blocks the first prompt of each idle period once;
/// Claude Code appends the blocked prompt to the notice (`Original prompt:`); resending it continues.
/// Built-in `idleCompaction` (100k+ tokens, compacts before expiry) is the automatic alternative.
fn cold_resume(payload: &Value, home: Option<&Path>) -> Option<Value> {
    // Interactive CLI only: a blocked `claude -p --resume` or SDK prompt would end the run unanswered.
    if std::env::var("CLAUDE_CODE_ENTRYPOINT").is_ok_and(|e| e != "cli") {
        return None;
    }
    let session = payload.get("session_id").and_then(Value::as_str)?;
    let state = ledger::state_file(home?, "budget", &format!("cold-{session}"))?;
    let (tokens, stamp) = newest_usage(Path::new(payload.get("transcript_path").and_then(Value::as_str)?))?;
    let stamp = stamp?;
    let last = epoch_seconds(&stamp)?;
    let now = i64::try_from(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs()).ok()?;
    let idle = now - last;
    if idle < limit("OMEGA_CONTEXT_COLD_SECONDS", COLD_SECONDS) as i64 || tokens < limit("OMEGA_CONTEXT_COLD_TOKENS", COLD_TOKENS) {
        return None;
    }
    let warned = ledger::read_bounded(&state).and_then(|b| serde_json::from_slice::<Value>(&b).ok());
    if warned.is_some_and(|v| v["cold_warned_for"] == stamp.as_str()) {
        return None; // Resent after the warning: continue in this session.
    }
    // Without a task file there is no cheap restart: /clear would lose the session's state.
    let task = session_task(Path::new(payload["transcript_path"].as_str()?), home?)?;
    let _ = ledger::write_private(&state, json!({"cold_warned_for": stamp}).to_string().as_bytes());
    Some(json!({"decision": "block", "reason": format!(
        "Session idle {}h{:02} with {}k tokens of context: the prompt cache has expired, so this prompt would \
         rewrite all {}k tokens to cache. Cheaper: /clear, then `'Ω-resume' {task}` and your prompt (check that \
         ~/.agents/tasks/active/{task}.md is current). To continue here anyway, send the prompt again.",
        idle / 3600, idle % 3600 / 60, tokens / 1000, tokens / 1000
    )}))
}

/// Newest active task file named in the transcript (last TASK_SCAN_BYTES) that still exists.
fn session_task(transcript: &Path, home: &Path) -> Option<String> {
    let mut file = std::fs::File::open(transcript).ok()?;
    let len = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(TASK_SCAN_BYTES))).ok()?;
    let mut tail = Vec::new();
    file.take(TASK_SCAN_BYTES).read_to_end(&mut tail).ok()?;
    let pattern = regex::bytes::Regex::new(r"tasks/active/([A-Za-z0-9][A-Za-z0-9._-]{0,100})\.md").ok()?;
    let names: Vec<String> = pattern
        .captures_iter(&tail)
        .filter_map(|c| std::str::from_utf8(&c[1]).ok().map(str::to_owned))
        .collect();
    let active = home.join(".agents/tasks/active");
    names.into_iter().rev().find(|name| active.join(format!("{name}.md")).is_file())
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
