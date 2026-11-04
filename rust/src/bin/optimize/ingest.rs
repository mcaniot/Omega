//! Normalize recorded Codex events, never execute their contents.
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};

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
