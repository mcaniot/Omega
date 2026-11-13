//! Normalize recorded Codex events, never execute their contents.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

/// Extract only returned read data, not commands, offsets or serialized images.
/// Content overlap is advisory: source files and transcripts are never changed.
pub fn is_read(call: &Value) -> bool {
    let input = call["input"].as_str().map(str::to_owned)
        .unwrap_or_else(|| call["input"].to_string());
    call["read"] == true || call["name"] == "Read" || input.split(|c: char| !c.is_alphanumeric() && c != '-')
        .any(|word| matches!(word, "cat" | "sed" | "head" | "tail" | "rg" | "Ω-read" | "Ω-inspect" | "omega-read" | "omega-inspect" | "inspect"))
}

pub fn read_text(call: &Value, result: &Value) -> Vec<String> {
    if !is_read(call) || result["is_error"] == true {
        return Vec::new();
    }
    fn text(value: &Value, depth: usize, output: &mut Vec<String>) {
        if depth > 8 { return; }
        match value {
            Value::String(s) => {
                if let Ok(parsed) = serde_json::from_str::<Value>(s) {
                    // JSON strings/numbers in a source file are still read data.
                    if parsed.is_array() || ["evidence", "stdout", "stderr", "output", "content", "fragments", "diagnostics", "preview"]
                        .iter().any(|key| parsed.get(key).is_some()) {
                        text(&parsed, depth + 1, output);
                        return;
                    }
                }
                for line in s.lines() {
                    if let Ok(parsed) = serde_json::from_str::<Value>(line) {
                        if parsed.get("output").is_some() || parsed.get("evidence").is_some() {
                            text(&parsed, depth + 1, output);
                            continue;
                        }
                    }
                    output.push(line.to_owned());
                }
            }
            Value::Array(items) => {
                for item in items { text(item, depth + 1, output); }
            }
            Value::Object(_) if value["type"] == "image" => {}
            Value::Object(_) => {
                for key in ["stdout", "stderr", "output", "text", "content", "fragments", "diagnostics", "preview"] {
                    if let Some(value) = value.get(key) { text(value, depth + 1, output); }
                }
            }
            _ => {}
        }
    }
    let mut output = Vec::new();
    text(&result["content"], 0, &mut output);
    output
}

/// Fingerprints of the source lines a read returned (Read-tool line numbers stripped, short lines ignored).
pub fn read_fingerprints(call: &Value, result: &Value) -> HashSet<String> {
    let mut found = HashSet::new();
    for text in read_text(call, result) {
        for line in text.lines() {
            // Read-tool line numbers are presentation, not source content.
            let line = if call["name"] == "Read" {
                line.split_once(['→', '\t']).filter(|(n, _)| !n.trim().is_empty() && n.trim().chars().all(|c| c.is_ascii_digit()))
                    .map_or(line, |(_, s)| s)
            } else { line };
            if line.len() >= 32 {
                found.insert(super::hex(&Sha256::digest(line.as_bytes())));
            }
        }
    }
    found
}

/// Transcript order of each successful file edit, by path.
pub fn edit_orders<'a>(
    calls: &'a HashMap<String, Value>,
    results: &HashMap<String, Value>,
    call_order: &HashMap<String, usize>,
) -> HashMap<&'a str, Vec<usize>> {
    let mut edits: HashMap<&str, Vec<usize>> = HashMap::new();
    for (id, call) in calls {
        if matches!(call["name"].as_str(), Some("Edit" | "Write" | "MultiEdit" | "NotebookEdit"))
            && results.get(id).is_some_and(|r| r["is_error"] != true) {
            if let Some(path) = call["input"]["file_path"].as_str() {
                edits.entry(path).or_default().push(call_order.get(id).copied().unwrap_or(0));
            }
        }
    }
    edits
}

/// A re-read is legitimate when a compaction or an edit of the file happened since the earlier read.
pub fn legit_reread(
    earlier: Option<usize>,
    source: Option<&str>,
    path: Option<&str>,
    order: usize,
    compaction_at: &[usize],
    edits: &HashMap<&str, Vec<usize>>,
) -> bool {
    earlier.is_some_and(|e| {
        compaction_at.iter().any(|c| e < *c && *c < order)
            || [source, path].into_iter().flatten().any(|p| {
                edits.get(p).is_some_and(|orders| orders.iter().any(|o| e < *o && *o < order))
            })
    })
}

/// Classify a tool error; only `real` needs investigation, the rest is workflow friction.
pub fn error_class(call: &Value, result: &Value) -> &'static str {
    let text = match &result["content"] {
        Value::String(s) => s.clone(),
        Value::Array(blocks) => blocks.iter().filter_map(|b| b["text"].as_str()).collect::<Vec<_>>().join("\n"),
        other => other.to_string(),
    };
    let lower = text.to_lowercase();
    let compact: String = lower.chars().filter(|c| !c.is_whitespace()).collect();
    let has = |needles: &[&str]| needles.iter().any(|n| lower.contains(&n.to_lowercase()));
    let input = call["input"].to_string();
    let words = |names: &[&str]| input.split(|c: char| !c.is_alphanumeric() && c != '-' && c != 'Ω')
        .any(|w| names.contains(&w));
    if has(&["no space left on device", "DATA_LOSS", "BLOCKED_DISK", "evidence write fail", "failed to write evidence"]) {
        return "data_loss";
    }
    if has(&["Unwrapped or noncanonical", "Large text file", "Large evidence", "PreToolUse", "policy block", "blocked by policy"]) {
        return "guard";
    }
    if ["exitcode141", "exitcode:141", "processexitedwithcode141", "\"exit_code\":141"].iter().any(|n| compact.contains(n)) {
        return "sigpipe";
    }
    let mut lines = lower.lines().map(str::trim);
    let first = lines.next().unwrap_or("");
    if ["exit code 1", "exit code: 1", "process exited with code 1"].contains(&first)
        && lines.all(str::is_empty) && words(&["grep", "egrep", "fgrep", "rg"]) {
        return "no_match";
    }
    if has(&["file has not been read yet"])
        || (words(&["Ω-inspect", "Ω-run", "omega-inspect"])
            && has(&["usage", "unrecognized", "invalid", "required", "no such file", "not found", "unknown argument"]))
        || (has(&["file does not exist", "no such file"])
            && call["input"]["file_path"].as_str().is_some_and(|p| p.contains("/evidence/"))) {
        return "wrapper_usage";
    }
    "real"
}

pub fn discover(root: &Path, recursive: bool, files: &mut Vec<PathBuf>) -> Result<(), String> {
    if !root.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_dir() && recursive {
            discover(&entry.path(), true, files)?;
        } else if kind.is_file() && entry.path().extension().is_some_and(|s| s == "jsonl") {
            files.push(entry.path());
        }
    }
    Ok(())
}

pub fn codex(row: Value) -> Value {
    let payload = &row["payload"];
    let timestamp = &row["timestamp"];
    match row["type"].as_str() {
        Some("session_meta" | "turn_context") => json!({"cwd": payload["cwd"]}),
        Some("compacted") => json!({"subtype": "compact_boundary", "uuid": timestamp}),
        Some("event_msg") if payload["type"] == "context_compacted" => {
            json!({"subtype": "compact_boundary", "uuid": timestamp})
        }
        Some("event_msg") if payload["type"] == "token_count" => {
            let usage = &payload["info"]["last_token_usage"];
            if !usage["input_tokens"].is_u64() {
                return Value::Null;
            }
            // Codex cached_input_tokens is a subset of input_tokens, not additive.
            json!({"type": "assistant", "timestamp": timestamp, "message": {
                "usage": {"input_tokens": usage["input_tokens"]}}})
        }
        Some("response_item") => match payload["type"].as_str() {
            Some("function_call" | "custom_tool_call") => {
                let input = payload["arguments"]
                    .as_str()
                    .and_then(|s| serde_json::from_str::<Value>(s).ok())
                    .unwrap_or_else(|| payload["input"].clone());
                json!({"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": payload["call_id"],
                    "name": payload["name"], "input": input}]}})
            }
            Some("function_call_output" | "custom_tool_call_output") => {
                let output = payload["output"].clone();
                let parsed = output
                    .as_str()
                    .and_then(|s| serde_json::from_str::<Value>(s).ok());
                let error = parsed.as_ref().is_some_and(|v| {
                    v["isError"] == true
                        || v["is_error"] == true
                        || v["exit_code"].as_i64().is_some_and(|code| code != 0)
                }) || output.as_str().is_some_and(|text| {
                    text.lines().any(|line| {
                        ["Process exited with code ", "Exit code: "]
                            .iter()
                            .any(|prefix| {
                                line.strip_prefix(prefix)
                                    .and_then(|code| code.trim().parse::<i64>().ok())
                                    .is_some_and(|code| code != 0)
                            })
                    })
                });
                json!({"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": payload["call_id"],
                    "content": output, "is_error": error}]}})
            }
            _ => Value::Null,
        },
        _ => Value::Null,
    }
}

pub fn findings(rows: &[Value], guard: &str) -> Vec<Value> {
    let mut findings = Vec::new();
    for row in rows {
        for (metric, action) in [
            (
                "large_unbounded_text_reads",
                "Use bounded Read ranges; enable the installed Claude Read guard.",
            ),
            (
                "large_evidence_text_reads",
                "Use Ω-inspect slices instead of rereading evidence logs.",
            ),
            (
                "large_tool_outputs",
                "Capture complete output with Ω-run and inspect only targeted slices.",
            ),
            (
                "repeated_exact_calls_extra",
                "Reuse previous evidence; rerun only after input or hypothesis changes.",
            ),
            (
                "overlapping_read_calls",
                "Returned read content overlaps earlier reads (possibly different ranges/commands). Reuse unchanged lines; inspect novel lines. Advisory only: never suppress reads or infer file freshness from overlap.",
            ),
            (
                "tool_errors",
                "Inspect the specific failure before retrying with changed input.",
            ),
            (
                "malformed_records",
                "Inspect transcript integrity; never repair or rewrite session records automatically.",
            ),
        ] {
            let count = row[metric].as_u64().unwrap_or(0);
            if count == 0 {
                continue;
            }
            let guard_fix = metric == "large_unbounded_text_reads" && row["provider"] == "claude";
            findings.push(json!({
                "provider": row["provider"], "session": row["session"],
                "transcript": row["transcript"], "start_offset": row["start_offset"],
                "end_offset": row["end_offset"], "metric": metric, "count": count,
                "action": action,
                "status": if guard_fix && guard == "enabled (updated)" { "applied" }
                    else if guard_fix && guard == "enabled" { "protected; follow-up required" }
                    else { "follow-up required" },
            }));
        }
        if row["context_last"].as_u64().unwrap_or(0) >= 100_000 {
            findings.push(json!({"provider": row["provider"], "session": row["session"],
                "metric": "context_last", "count": row["context_last"],
                "action": "Checkpoint verified decisions, evidence and next action before manual compaction or a fresh session.",
                "status": "follow-up required"}));
        }
    }
    findings
}
