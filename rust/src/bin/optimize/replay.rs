//! Read-only counterfactual replay: would old Claude sessions have used fewer tokens with the
//! Read guard, workflow-retry fixes and context budget? Never loads/saves checkpoint state and
//! never touches settings; the only write is an optional new `--report` file.
//!
//! Model per session (requests deduplicated by message id, last record wins):
//!   baseline = sum(context_i + output_i), context = input + cache_creation + cache_read.
//!   Tool-result tokens = result bytes / bytes-per-token, calibrated per session as the median of
//!   bytes / context-delta over request pairs separated by exactly one tool result.
//!   R1: a Read whose lines are all avoidable re-reads becomes a DENY_TOKENS deny message; the
//!       saving leaves the context of every later request.
//!   R2: a sigpipe/no_match/wrapper_usage error followed by another call of the same tool is
//!       removed with its request (context row, output, result tokens in later contexts).
//!       Guard and real errors save nothing.
//!   R3: when the adjusted context reaches the reset threshold a new segment starts: later contexts
//!       become first-request context + RESUME_TOKENS + growth since the cut. A baseline compaction
//!       inside a segment keeps the baseline's own drop; compaction-driven legit re-reads stay
//!       charged (conservative). A cut is an automatic handoff (new session resumes from the task
//!       file). Honest accounting: the first Read/Edit/MultiEdit/NotebookEdit after a cut of a file
//!       read in an earlier segment re-reads it: its largest earlier Read result is added to every
//!       later context of the segment, plus one extra request (context at that point, output 0).
use super::ingest;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;

/// Fallback when a session has no single-result context delta to calibrate from.
const DEFAULT_BYTES_PER_TOKEN: f64 = 3.5;
/// Smaller results are dominated by per-turn overhead (reminders, framing), so they skew calibration.
const MIN_CALIBRATION_BYTES: usize = 1000;
/// Size of the message that replaces a denied re-read.
const DENY_TOKENS: f64 = 40.0;
/// Context at which the budget hook forces an automatic handoff and resume.
const HARD_RESET_TOKENS: u64 = 130_000;
/// Context of a fresh session after `omega resume`, on top of the first-request prefix.
const RESUME_TOKENS: f64 = 3_000.0;
const DEFAULT_SESSIONS: usize = 12;
const MAX_SESSIONS: usize = 50;
const TOP_SESSIONS: usize = 5;

struct Request {
    id: String,
    index: usize,
    context: u64,
    output: u64,
}

/// A file path touched by a call; `read_tokens` is set for a Read with a result.
struct Access {
    path: String,
    read_tokens: Option<f64>,
}

fn result_bytes(result: &Value) -> usize {
    match &result["content"] {
        Value::String(s) => s.len(),
        Value::Array(blocks) => blocks.iter().filter(|b| b["type"] == "text").filter_map(|b| b["text"].as_str()).map(str::len).sum(),
        _ => 0,
    }
}

fn median(mut values: Vec<f64>) -> f64 {
    values.sort_by(f64::total_cmp);
    match values.len() {
        0 => 0.0,
        n if n % 2 == 1 => values[n / 2],
        n => (values[n / 2 - 1] + values[n / 2]) / 2.0,
    }
}

/// Cost of the session with `savings` (result index, tokens) removed from later contexts,
/// `removed` requests dropped and, when `threshold` is set, the budget segments applied.
fn simulate(
    requests: &[Request],
    savings: &[(usize, f64)],
    removed: &[bool],
    threshold: Option<u64>,
    compaction_at: &[usize],
    accesses: &[Vec<Access>],
) -> (f64, u64, u64) {
    let prefix = requests.first().map_or(0.0, |r| r.context as f64);
    let (mut cost, mut segments, mut rereads) = (0.0, 1, 0);
    // Largest Read result per file in earlier segments / the current one, and files already re-read.
    let (mut earlier, mut current): (HashMap<&str, f64>, HashMap<&str, f64>) = (HashMap::new(), HashMap::new());
    let mut charged: HashSet<&str> = HashSet::new();
    let (mut shift, mut last_index) = (0.0, 0);
    for (i, request) in requests.iter().enumerate() {
        if removed[i] { continue; }
        let saved: f64 = savings.iter().filter(|(at, _)| *at < request.index).map(|(_, t)| t).sum();
        let base = (request.context as f64 - saved).max(0.0);
        if compaction_at.iter().any(|c| last_index < *c && *c < request.index) {
            shift = 0.0;
        }
        last_index = request.index;
        let effective = (base + shift).max(0.0);
        cost += effective + request.output as f64;
        if threshold.is_some_and(|limit| effective >= limit as f64) {
            segments += 1;
            shift = prefix + RESUME_TOKENS - base;
            for (path, tokens) in current.drain() {
                let best = earlier.entry(path).or_insert(0.0);
                *best = best.max(tokens);
            }
            charged.clear();
        }
        // Accesses of this request run after a cut triggered by it, so they belong to the new segment.
        for access in &accesses[i] {
            if let Some(&tokens) = earlier.get(access.path.as_str()).filter(|_| charged.insert(access.path.as_str())) {
                cost += (base + shift).max(0.0);
                shift += tokens;
                rereads += 1;
            }
            if let Some(tokens) = access.read_tokens {
                let best = current.entry(access.path.as_str()).or_insert(0.0);
                *best = best.max(tokens);
            }
        }
    }
    (cost, segments, rereads)
}

fn replay_session(path: &std::path::Path, threshold: u64) -> Result<Option<Value>, String> {
    let data = fs::read(path).map_err(|e| e.to_string())?;
    let mut messages: HashMap<String, (usize, u64, u64)> = HashMap::new();
    let (mut calls, mut call_order, mut call_message) = (HashMap::new(), HashMap::new(), HashMap::new());
    let (mut results, mut result_order): (HashMap<String, Value>, HashMap<String, usize>) = (HashMap::new(), HashMap::new());
    let mut compaction_at = Vec::new();
    // The last split piece is empty or an unfinished record of an active writer.
    let mut lines: Vec<&[u8]> = data.split(|b| *b == b'\n').collect();
    lines.pop();
    for (n, line) in lines.into_iter().enumerate() {
        let index = n + 1;
        let Ok(row) = serde_json::from_slice::<Value>(line) else { continue };
        if row["subtype"].as_str().is_some_and(|s| s.contains("compact")) {
            compaction_at.push(index);
        }
        let mut message_id = None;
        if row["type"] == "assistant" && row["message"]["usage"].is_object() {
            let usage = &row["message"]["usage"];
            let get = |k: &str| usage[k].as_u64().unwrap_or(0);
            let context = get("input_tokens") + get("cache_creation_input_tokens") + get("cache_read_input_tokens");
            let id = row["message"]["id"].as_str().or_else(|| row["uuid"].as_str()).map_or_else(|| index.to_string(), str::to_owned);
            messages.insert(id.clone(), (index, context, get("output_tokens")));
            message_id = Some(id);
        }
        if !matches!(row["type"].as_str(), Some("assistant" | "user")) { continue; }
        for block in row["message"]["content"].as_array().into_iter().flatten() {
            if block["type"] == "tool_use" {
                if let Some(id) = block["id"].as_str() {
                    calls.insert(id.to_owned(), block.clone());
                    call_order.entry(id.to_owned()).or_insert(index);
                    if let Some(message) = &message_id { call_message.entry(id.to_owned()).or_insert(message.clone()); }
                }
            } else if block["type"] == "tool_result" {
                if let Some(id) = block["tool_use_id"].as_str() {
                    results.insert(id.to_owned(), block.clone());
                    result_order.entry(id.to_owned()).or_insert(index);
                }
            }
        }
    }
    let mut requests: Vec<Request> = messages.into_iter()
        .map(|(id, (index, context, output))| Request { id, index, context, output }).collect();
    requests.sort_by_key(|r| r.index);
    if requests.is_empty() { return Ok(None); }
    let baseline = requests.iter().map(|r| r.context + r.output).sum::<u64>() as f64;

    // Calibration: request pairs whose context growth is driven by exactly one tool result.
    let mut result_marks: Vec<(usize, usize)> = results.iter()
        .map(|(id, r)| (result_order.get(id).copied().unwrap_or(0), result_bytes(r))).collect();
    result_marks.sort();
    let mut samples = Vec::new();
    for pair in requests.windows(2) {
        let (from, to) = (pair[0].index, pair[1].index);
        let between: Vec<_> = result_marks.iter().filter(|(at, _)| from < *at && *at < to).collect();
        // The next context also contains the previous assistant output; only the remainder is the result.
        let delta = pair[1].context as f64 - pair[0].context as f64 - pair[0].output as f64;
        if between.len() == 1 && between[0].1 >= MIN_CALIBRATION_BYTES && delta > 0.0 && !compaction_at.iter().any(|c| from < *c && *c < to) {
            samples.push(between[0].1 as f64 / delta);
        }
    }
    let bytes_per_token = if samples.is_empty() { DEFAULT_BYTES_PER_TOKEN } else { median(samples.clone()) };
    let tokens_of = |id: &String| results.get(id).map_or(0.0, |r| result_bytes(r) as f64 / bytes_per_token);

    // R1 and the shared ordered walk over calls.
    let edits = ingest::edit_orders(&calls, &results, &call_order);
    let mut ordered: Vec<(&String, &Value)> = calls.iter().collect();
    ordered.sort_by_key(|(id, _)| (call_order.get(*id).copied().unwrap_or(0), (*id).clone()));
    let mut seen: HashMap<String, (Option<usize>, Option<String>)> = HashMap::new();
    let mut r1_savings = Vec::new();
    for (id, call) in &ordered {
        let Some(result) = results.get(*id) else { continue };
        let order = call_order.get(*id).copied().unwrap_or(0);
        let path = call["input"]["file_path"].as_str().map(str::to_owned);
        let fingerprints = ingest::read_fingerprints(call, result);
        if fingerprints.is_empty() { continue; }
        let avoidable = call["name"] == "Read" && fingerprints.iter().all(|f| {
            seen.get(f).is_some_and(|(earlier, source)| {
                !ingest::legit_reread(*earlier, source.as_deref(), path.as_deref(), order, &compaction_at, &edits)
            })
        });
        if avoidable {
            r1_savings.push((result_order.get(*id).copied().unwrap_or(0), (tokens_of(id) - DENY_TOKENS).max(0.0)));
        }
        for f in fingerprints { seen.insert(f, (Some(order), path.clone())); }
    }

    // R2: workflow errors retried with the same tool; a request is removed when all its calls are such errors.
    let mut last_call: HashMap<&str, usize> = HashMap::new();
    for (id, call) in &calls {
        let order = call_order.get(id).copied().unwrap_or(0);
        let slot = last_call.entry(call["name"].as_str().unwrap_or("")).or_insert(0);
        *slot = (*slot).max(order);
    }
    let mut per_request: HashMap<&str, (usize, usize)> = HashMap::new();
    let mut retried = Vec::new();
    for (id, call) in &calls {
        let Some(message) = call_message.get(id) else { continue };
        let entry = per_request.entry(message.as_str()).or_default();
        entry.0 += 1;
        let order = call_order.get(id).copied().unwrap_or(0);
        let eligible = results.get(id).is_some_and(|r| {
            r["is_error"] == true && matches!(ingest::error_class(call, r), "sigpipe" | "no_match" | "wrapper_usage")
        }) && last_call.get(call["name"].as_str().unwrap_or("")).is_some_and(|last| *last > order);
        if eligible {
            entry.1 += 1;
            retried.push((message.as_str(), id));
        }
    }
    let removed: Vec<bool> = requests.iter().map(|r| per_request.get(r.id.as_str()).is_some_and(|(all, bad)| *all > 0 && all == bad)).collect();
    let mut r2_savings = r1_savings.clone();
    for (message, id) in retried {
        if per_request[message].0 == per_request[message].1 {
            r2_savings.push((result_order.get(id).copied().unwrap_or(0), tokens_of(id)));
        }
    }

    let position: HashMap<&str, usize> = requests.iter().enumerate().map(|(i, r)| (r.id.as_str(), i)).collect();
    let mut accesses: Vec<Vec<Access>> = requests.iter().map(|_| Vec::new()).collect();
    for (id, call) in &ordered {
        let name = call["name"].as_str().unwrap_or("");
        let key = if name == "NotebookEdit" { "notebook_path" } else { "file_path" };
        let (Some(path), true) = (call["input"][key].as_str(), matches!(name, "Read" | "Edit" | "MultiEdit" | "NotebookEdit")) else { continue };
        let Some(&slot) = call_message.get(*id).and_then(|m| position.get(m.as_str())) else { continue };
        let read_tokens = (name == "Read" && results.contains_key(*id)).then(|| tokens_of(id));
        accesses[slot].push(Access { path: path.to_owned(), read_tokens });
    }
    let none = vec![false; requests.len()];
    let no_accesses: Vec<Vec<Access>> = requests.iter().map(|_| Vec::new()).collect();
    let (cost1, _, _) = simulate(&requests, &r1_savings, &none, None, &compaction_at, &no_accesses);
    let (cost2, _, _) = simulate(&requests, &r2_savings, &removed, None, &compaction_at, &no_accesses);
    let (gross3, _, _) = simulate(&requests, &r2_savings, &removed, Some(threshold), &compaction_at, &no_accesses);
    let (cost3, segments, rereads) = simulate(&requests, &r2_savings, &removed, Some(threshold), &compaction_at, &accesses);
    let pct = |saved: f64| if baseline > 0.0 { (saved / baseline * 10_000.0).round() / 100.0 } else { 0.0 };
    Ok(Some(json!({
        "session": path.file_stem().and_then(|s| s.to_str()).unwrap_or("?"),
        "requests": requests.len(),
        "baseline_tokens": baseline.round() as u64,
        "counterfactual_tokens": cost3.round() as u64,
        "reduction_pct": pct(baseline - cost3),
        "savings": {"r1": (baseline - cost1).round() as i64, "r2": (cost1 - cost2).round() as i64, "r3": (cost2 - cost3).round() as i64},
        "r3_gross": (cost2 - gross3).round() as i64,
        "r3_reread_charge": (cost3 - gross3).round() as i64,
        "post_cut_rereads": rereads,
        "avoidable_rereads": r1_savings.len(),
        "segments": segments,
        "handoffs": segments - 1,
        "bytes_per_token": bytes_per_token,
        "calibration_samples": samples.len(),
    })))
}

pub fn run(args: Vec<String>) -> Result<(), String> {
    let (mut limit, mut json_output, mut report, mut threshold) = (DEFAULT_SESSIONS, false, None, HARD_RESET_TOKENS);
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" => json_output = true,
            "--sessions" => {
                limit = args.next().and_then(|v| v.parse::<usize>().ok()).filter(|n| (1..=MAX_SESSIONS).contains(n))
                    .ok_or("--sessions requires 1..50")?;
            }
            "--handoff-tokens" | "--soft-reset-tokens" => {
                let n = args.next().and_then(|v| v.parse::<u64>().ok()).filter(|n| *n > 0).ok_or("--handoff-tokens requires a positive integer")?;
                threshold = n;
            }
            "--report" => report = Some(PathBuf::from(args.next().ok_or("--report requires a new JSON file path")?)),
            _ => return Err(format!("Unknown argument: {arg}")),
        }
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is required")?);
    let root = home.join(".claude/projects");
    let mut paths = Vec::new();
    if root.exists() {
        for directory in fs::read_dir(&root).map_err(|e| e.to_string())? {
            let directory = directory.map_err(|e| e.to_string())?;
            if directory.file_type().map_err(|e| e.to_string())?.is_dir() {
                ingest::discover(&directory.path(), false, &mut paths)?;
            }
        }
    }
    let mut files = Vec::new();
    for path in paths {
        let modified = path.metadata().and_then(|m| m.modified()).map_err(|e| e.to_string())?;
        files.push((modified, path));
    }
    files.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    let mut rows = Vec::new();
    for (_, path) in files.into_iter().take(limit) {
        rows.extend(replay_session(&path, threshold)?);
    }
    let sum = |f: &dyn Fn(&Value) -> f64| rows.iter().map(f).sum::<f64>();
    let (baseline, counterfactual) = (sum(&|r| r["baseline_tokens"].as_f64().unwrap_or(0.0)), sum(&|r| r["counterfactual_tokens"].as_f64().unwrap_or(0.0)));
    let rule = |k: &str| sum(&|r| r["savings"][k].as_f64().unwrap_or(0.0));
    let field = |k: &str| sum(&|r| r[k].as_f64().unwrap_or(0.0));
    let (rereads, avoidable) = (field("post_cut_rereads"), field("avoidable_rereads"));
    let mut top: Vec<&Value> = rows.iter().collect();
    top.sort_by_key(|r| std::cmp::Reverse(r["baseline_tokens"].as_i64().unwrap_or(0) - r["counterfactual_tokens"].as_i64().unwrap_or(0)));
    let output = json!({
        "model": {"default_bytes_per_token": DEFAULT_BYTES_PER_TOKEN, "deny_tokens": DENY_TOKENS,
                  "reset_tokens": threshold, "handoff_tokens": threshold, "resume_tokens": RESUME_TOKENS},
        "sessions": rows,
        "aggregate": {
            "sessions": rows.len(),
            "baseline_total": baseline,
            "counterfactual_total": counterfactual,
            "baseline_median": median(rows.iter().map(|r| r["baseline_tokens"].as_f64().unwrap_or(0.0)).collect()),
            "counterfactual_median": median(rows.iter().map(|r| r["counterfactual_tokens"].as_f64().unwrap_or(0.0)).collect()),
            "reduction_pct_median": median(rows.iter().map(|r| r["reduction_pct"].as_f64().unwrap_or(0.0)).collect()),
            "ratio": if counterfactual > 0.0 { baseline / counterfactual } else { 0.0 },
            "target_ratio": 3.0,
            "savings": {"r1": rule("r1"), "r2": rule("r2"), "r3": rule("r3")},
            "r3_gross": field("r3_gross"),
            "r3_reread_charge": field("r3_reread_charge"),
            "post_cut_rereads": rereads,
            "handoffs": field("handoffs"),
            // Counterfactual: R1 already blocks every avoidable re-read, so only post-handoff re-reads remain.
            "zero_reread": rereads == 0.0,
            "baseline_avoidable_rereads": avoidable,
            "top_sessions": top.iter().take(TOP_SESSIONS).collect::<Vec<_>>(),
        },
    });
    if let Some(path) = report {
        let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path).map_err(|e| e.to_string())?;
        serde_json::to_writer_pretty(&mut file, &output).map_err(|e| e.to_string())?;
        file.write_all(b"\n").and_then(|_| file.sync_all()).map_err(|e| e.to_string())?;
    }
    if json_output {
        println!("{output}");
        return Ok(());
    }
    println!("SESSION   REQS  BASELINE    COUNTERFACTUAL  REDUCTION  R1        R2        R3        SEG  B/TOK");
    for row in output["sessions"].as_array().into_iter().flatten() {
        let session: String = row["session"].as_str().unwrap_or("?").chars().take(8).collect();
        println!("{session:8}  {:<4}  {:<10}  {:<14}  {:<8.2}%  {:<8}  {:<8}  {:<8}  {:<3}  {:.2}",
            row["requests"], row["baseline_tokens"], row["counterfactual_tokens"], row["reduction_pct"].as_f64().unwrap_or(0.0),
            row["savings"]["r1"], row["savings"]["r2"], row["savings"]["r3"], row["segments"], row["bytes_per_token"].as_f64().unwrap_or(0.0));
    }
    let a = &output["aggregate"];
    println!("Total baseline {} -> counterfactual {} (ratio {:.2}, target >= 3); median {} -> {}; savings r1/r2/r3 {}/{}/{}",
        a["baseline_total"], a["counterfactual_total"], a["ratio"].as_f64().unwrap_or(0.0),
        a["baseline_median"], a["counterfactual_median"], a["savings"]["r1"], a["savings"]["r2"], a["savings"]["r3"]);
    Ok(())
}
