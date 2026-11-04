//! Repeatable local context audit; never rewrites or compacts conversations.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

#[path = "optimize/ingest.rs"]
mod ingest;

fn audit(
    path: &Path,
    provider: &str,
    start: u64,
    pending: &Value,
) -> Result<(Value, u64, Value), String> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    file.seek(SeekFrom::Start(start))
        .map_err(|e| e.to_string())?;
    let mut reader = BufReader::new(file);
    let mut offset = start;
    let mut messages = HashMap::new();
    let mut calls = HashMap::new();
    let mut call_order = HashMap::new();
    let mut read_history: VecDeque<String> = pending["_read_fingerprints"].as_array()
        .into_iter().flatten().filter_map(|v| v.as_str().map(str::to_owned)).take(2048).collect();
    if let Some(previous) = pending.as_object() {
        for (id, call) in previous {
            if id == "_read_fingerprints" { continue; }
            calls.insert(id.clone(), call.clone());
        }
    }
    let carried: HashSet<_> = calls.keys().cloned().collect();
    let mut results = HashMap::new();
    let mut compactions = HashSet::new();
    let mut malformed = 0;
    let mut project = String::new();
    let mut index = 0;
    loop {
        let mut line = Vec::new();
        let size = reader
            .read_until(b'\n', &mut line)
            .map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        // An active writer may not have finished this record. Do not consume it.
        if !line.ends_with(b"\n") {
            break;
        }
        offset += size as u64;
        index += 1;
        let raw: Value = match serde_json::from_slice(&line) {
            Ok(row) => row,
            Err(_) => {
                malformed += 1;
                continue;
            }
        };
        let row = if provider == "codex" {
            ingest::codex(raw)
        } else {
            raw
        };
        if let Some(cwd) = row["cwd"].as_str() {
            project = cwd.to_owned();
        }
        if row["subtype"]
            .as_str()
            .is_some_and(|s| s.contains("compact"))
        {
            compactions.insert(
                row["uuid"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| index.to_string()),
            );
        }
        if row["type"] == "assistant" && row["message"]["usage"].is_object() {
            let usage = &row["message"]["usage"];
            let context: u64 = [
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ]
            .iter()
            .map(|k| usage[k].as_u64().unwrap_or(0))
            .sum();
            let id = row["message"]["id"]
                .as_str()
                .or_else(|| row["uuid"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| index.to_string());
            messages.insert(id, (index, context, row["timestamp"].clone()));
            if let Some(cwd) = row["cwd"].as_str() {
                project = cwd.to_owned();
            }
        }
        if !matches!(row["type"].as_str(), Some("assistant" | "user")) {
            continue;
        }
        if let Some(blocks) = row["message"]["content"].as_array() {
            for block in blocks {
                if block["type"] == "tool_use" {
                    if let Some(id) = block["id"].as_str() {
                        calls.insert(id.to_owned(), block.clone());
                        call_order.entry(id.to_owned()).or_insert(index);
                    }
                } else if block["type"] == "tool_result" {
                    if let Some(id) = block["tool_use_id"].as_str() {
                        results.insert(id.to_owned(), block.clone());
                    }
                }
            }
        }
    }
    let mut ordered: Vec<_> = messages.values().collect();
    ordered.sort_by_key(|v| v.0);
    let mut signatures = HashSet::new();
    let mut repeated = 0;
    let mut large_unbounded_reads = 0;
    let mut large_evidence_reads = 0;
    let mut large_tool_outputs = 0;
    let mut seen_reads: HashSet<String> = read_history.iter().cloned().collect();
    let (mut read_calls, mut overlapping_read_calls, mut repeated_read_lines, mut novel_read_lines) = (0, 0, 0, 0);
    let mut sorted_calls: Vec<_> = calls.iter().collect();
    sorted_calls.sort_by_key(|(id, _)| (call_order.get(*id).copied().unwrap_or(0), *id));
    for (id, call) in sorted_calls {
        let signature = format!(
            "{:x}",
            Sha256::digest(format!("{}:{}", call["name"], call["input"]).as_bytes())
        );
        if !carried.contains(id) && !signatures.insert(signature) {
            repeated += 1;
        }
        let Some(result) = results.get(id) else {
            continue;
        };
        let mut this_read = HashSet::new();
        for text in ingest::read_text(call, result) {
            for line in text.lines() {
                // Read-tool line numbers are presentation, not source content.
                let line = if call["name"] == "Read" {
                    line.split_once('→').filter(|(n, _)| !n.trim().is_empty() && n.trim().chars().all(|c| c.is_ascii_digit()))
                        .map_or(line, |(_, s)| s)
                } else { line };
                if line.len() >= 32 {
                    this_read.insert(format!("{:x}", Sha256::digest(line.as_bytes())));
                }
            }
        }
        if !this_read.is_empty() {
            read_calls += 1;
            let overlap = this_read.intersection(&seen_reads).count();
            repeated_read_lines += overlap;
            novel_read_lines += this_read.len() - overlap;
            if overlap > 0 { overlapping_read_calls += 1; }
            let mut fingerprints: Vec<_> = this_read.into_iter().collect();
            fingerprints.sort();
            for fingerprint in fingerprints {
                if seen_reads.insert(fingerprint.clone()) { read_history.push_back(fingerprint); }
                if read_history.len() > 2048 {
                    if let Some(old) = read_history.pop_front() { seen_reads.remove(&old); }
                }
            }
        }
        // Images are not text: never turn their serialized bytes into a token estimate.
        let text_bytes = match &result["content"] {
            Value::String(s) => s.len(),
            Value::Array(blocks) => blocks
                .iter()
                .filter(|b| b["type"] == "text")
                .filter_map(|b| b["text"].as_str())
                .map(str::len)
                .sum(),
            _ => 0,
        };
        if text_bytes > 8192 {
            large_tool_outputs += 1;
            if call["name"] != "Read" {
                continue;
            }
            if call["input"]["limit"].is_null() {
                large_unbounded_reads += 1;
            }
            if call["input"]["file_path"]
                .as_str()
                .is_some_and(|p| p.contains("/.agents/tasks/evidence/"))
            {
                large_evidence_reads += 1;
            }
        }
    }
    // Carry only unfinished tool metadata across the checkpoint, never tool output or commands.
    let mut next_pending = json!({});
    if !read_history.is_empty() {
        next_pending["_read_fingerprints"] = json!(read_history);
    }
    for (id, call) in &calls {
        if !results.contains_key(id) {
            next_pending[id] = json!({"name": call["name"], "read": ingest::is_read(call), "input": {
                "file_path": call["input"]["file_path"], "limit": call["input"]["limit"]}});
        }
    }
    Ok((
        json!({
            "provider": provider,
            "transcript": path,
            "start_offset": start,
            "end_offset": offset,
            "session": path.file_stem().and_then(|p| p.to_str()).unwrap_or("?"),
            "project": project,
            "last_assistant_utc": ordered.last().map(|v| &v.2),
            "requests": ordered.len(),
            "context_first": ordered.first().map(|v| v.1).unwrap_or(0),
            "context_last": ordered.last().map(|v| v.1).unwrap_or(0),
            "context_peak": ordered.iter().map(|v| v.1).max().unwrap_or(0),
            "compactions": compactions.len(),
            "tool_calls": calls.keys().filter(|id| !carried.contains(*id)).count(),
            "tool_errors": results.values().filter(|r| r["is_error"] == true).count(),
            "repeated_exact_calls_extra": repeated,
            "read_calls": read_calls,
            "overlapping_read_calls": overlapping_read_calls,
            "repeated_read_lines": repeated_read_lines,
            "novel_read_lines": novel_read_lines,
            "large_unbounded_text_reads": large_unbounded_reads,
            "large_evidence_text_reads": large_evidence_reads,
            "large_tool_outputs": large_tool_outputs,
            "malformed_records": malformed,
        }),
        offset,
        next_pending,
    ))
}

fn prefix_digest(path: &Path, length: u64) -> Result<String, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut input = file.take(length);
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let size = input.read(&mut buffer).map_err(|e| e.to_string())?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn timestamp() -> Result<u64, String> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|time| time.as_secs())
        .map_err(|e| e.to_string())
}

fn fingerprint(path: &Path) -> Result<Value, String> {
    let m = path.metadata().map_err(|e| e.to_string())?;
    Ok(
        json!({"size": m.len(), "mtime_seconds": m.mtime(), "mtime_nanoseconds": m.mtime_nsec(),
        "ctime_seconds": m.ctime(), "ctime_nanoseconds": m.ctime_nsec(), "device": m.dev(), "inode": m.ino()}),
    )
}

fn load_state(home: &Path) -> Result<(File, PathBuf, Value), String> {
    let directory = home.join(".agents/tasks/state");
    fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(directory.join("omega-optimize.lock"))
        .map_err(|e| e.to_string())?;
    // The OS releases this lock even on interruption; no stale-lock cleanup is needed.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("Another omega optimize run holds the state lock; retry later".to_owned());
    }
    let path = directory.join("omega-optimize.json");
    let state = match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
            .map_err(|e| format!("Invalid optimize state: {e}"))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({"version": 1, "sessions": {}}),
        Err(e) => return Err(e.to_string()),
    };
    if state["version"] != 1 || !state["sessions"].is_object() {
        return Err("Unsupported optimize state schema; existing state preserved".to_owned());
    }
    Ok((lock, path, state))
}

fn save_state(path: &Path, state: &Value) -> Result<(), String> {
    let target = if fs::symlink_metadata(path).is_ok() {
        path.canonicalize().map_err(|e| e.to_string())?
    } else {
        path.to_owned()
    };
    let temp = target.with_file_name(format!(".omega-optimize-{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)
        .map_err(|e| e.to_string())?;
    let result = (|| -> Result<(), String> {
        serde_json::to_writer_pretty(&mut file, state).map_err(|e| e.to_string())?;
        file.write_all(b"\n")
            .and_then(|_| file.sync_all())
            .map_err(|e| e.to_string())?;
        fs::rename(&temp, &target).map_err(|e| e.to_string())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

struct SessionBatch {
    rows: Vec<Value>,
    unchanged: usize,
    unstable: usize,
    recorded: usize,
}

fn sessions(
    home: &Path,
    limit: usize,
    state: &mut Value,
    force: bool,
) -> Result<SessionBatch, String> {
    let mut batch = SessionBatch {
        rows: Vec::new(),
        unchanged: 0,
        unstable: 0,
        recorded: 0,
    };
    let mut files = Vec::new();
    for (provider, root) in [
        ("claude", home.join(".claude/projects")),
        ("codex", home.join(".codex/sessions")),
    ] {
        let mut paths = Vec::new();
        if provider == "claude" && root.exists() {
            for directory in fs::read_dir(&root).map_err(|e| e.to_string())? {
                let directory = directory.map_err(|e| e.to_string())?;
                if directory.file_type().map_err(|e| e.to_string())?.is_dir() {
                    ingest::discover(&directory.path(), false, &mut paths)?;
                }
            }
        } else {
            ingest::discover(&root, true, &mut paths)?;
        }
        for path in paths {
            let relative = path
                .strip_prefix(&root)
                .map_err(|e| e.to_string())?
                .to_string_lossy();
            // Retain legacy Claude keys; namespace Codex to avoid cross-provider collisions.
            let key = if provider == "claude" {
                relative.into_owned()
            } else {
                format!("codex/{relative}")
            };
            let modified = path
                .metadata()
                .and_then(|m| m.modified())
                .map_err(|e| e.to_string())?;
            files.push((modified, path, key, provider));
        }
    }
    files.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    for (_, path, key, provider) in files {
        let before = fingerprint(&path)?;
        if !force && state["sessions"][&key]["fingerprint"] == before {
            batch.unchanged += 1;
            continue;
        }
        // Unchanged files must not consume the changed-session budget.
        if batch.rows.len() >= limit {
            continue;
        }
        let previous = &state["sessions"][&key];
        let previous_offset = previous["offset"].as_u64().unwrap_or(0);
        let append_only = previous_offset <= before["size"].as_u64().unwrap_or(0)
            && previous["fingerprint"]["device"] == before["device"]
            && previous["fingerprint"]["inode"] == before["inode"]
            && previous["prefix_sha256"].as_str().is_some()
            && previous["prefix_sha256"] == prefix_digest(&path, previous_offset)?;
        let start = if !force && append_only {
            previous_offset
        } else {
            0
        };
        let pending = if start > 0 {
            previous["pending_calls"].clone()
        } else {
            Value::Null
        };
        let (mut row, offset, pending) = audit(&path, provider, start, &pending)?;
        if row["project"] == "" {
            row["project"] = previous["project"].clone();
        }
        row["since_unix_seconds"] = if start > 0 {
            previous["last_analyzed_unix_seconds"].clone()
        } else {
            Value::Null
        };
        row["analysis_scope"] = json!(if start > 0 {
            "since previous checkpoint"
        } else {
            "full baseline"
        });
        let project = row["project"].clone();
        batch.rows.push(row);
        let digest = prefix_digest(&path, offset)?;
        if fingerprint(&path)? == before {
            state["sessions"][&key] = json!({"fingerprint": before, "last_analyzed_unix_seconds": timestamp()?,
                    "offset": offset, "prefix_sha256": digest, "pending_calls": pending, "project": project});
            batch.recorded += 1;
        } else {
            batch.unstable += 1;
        }
    }
    Ok(batch)
}

fn hook_enabled(settings: &Value, command: &str) -> bool {
    settings["hooks"]["PreToolUse"]
        .as_array()
        .is_some_and(|groups| {
            groups.iter().any(|g| {
                matches!(g["matcher"].as_str(), Some("Read" | "*"))
                    && g["hooks"].as_array().is_some_and(|hooks| {
                        hooks
                            .iter()
                            .any(|h| h["type"] == "command" && h["command"] == command)
                    })
            })
        })
}

fn configure(home: &Path, apply: bool) -> Result<&'static str, String> {
    let command = home.join(".agents/tools/context_guard");
    let command_text = command.to_str().ok_or("Invalid HOME path")?;
    let dest = home.join(".claude/settings.json");
    let original = match fs::read(&dest) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => b"{}".to_vec(),
        Err(e) => return Err(e.to_string()),
    };
    let mut settings: Value =
        serde_json::from_slice(&original).map_err(|e| format!("Invalid Claude settings: {e}"))?;
    if !settings.is_object() {
        return Err("Claude settings must be an object".to_owned());
    }
    if settings["disableAllHooks"] == true {
        return if apply {
            Err("Claude hooks are globally disabled; preference preserved".to_owned())
        } else {
            Ok("disabled globally; preference preserved")
        };
    }
    let installed = command
        .metadata()
        .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0);
    if !installed {
        return if apply {
            Err(
                "Read guard missing or not executable; run install.py --context-only --apply first"
                    .to_owned(),
            )
        } else {
            Ok("not installed; run install.py --context-only --apply")
        };
    }
    if hook_enabled(&settings, command_text) {
        return Ok("enabled");
    }
    if !apply {
        return Ok("missing; use --apply to enable the installed Read guard");
    }
    let object = settings
        .as_object_mut()
        .ok_or("Claude settings must be an object")?;
    let hooks = object
        .entry("hooks")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("hooks must be an object")?;
    let groups = hooks
        .entry("PreToolUse")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or("PreToolUse must be an array")?;
    groups
        .push(json!({"matcher": "Read", "hooks": [{"type": "command", "command": command_text}]}));
    let target = if fs::symlink_metadata(&dest).is_ok() {
        dest.canonicalize().map_err(|e| e.to_string())?
    } else {
        dest.clone()
    };
    fs::create_dir_all(target.parent().ok_or("Invalid settings path")?)
        .map_err(|e| e.to_string())?;
    let temp = target.with_file_name(format!(".settings.optimize-{}.tmp", std::process::id()));
    let mode = target
        .metadata()
        .map(|m| m.permissions().mode() & 0o777)
        .unwrap_or(0o600);
    // create_new avoids clobbering another invocation; only clean up our own temp file.
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(&temp)
        .map_err(|e| e.to_string())?;
    let result = (|| -> Result<(), String> {
        file.set_permissions(fs::Permissions::from_mode(mode))
            .map_err(|e| e.to_string())?;
        let mut bytes = serde_json::to_vec_pretty(&settings).map_err(|e| e.to_string())?;
        bytes.push(b'\n');
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|e| e.to_string())?;
        let current = match fs::read(&dest) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => b"{}".to_vec(),
            Err(e) => return Err(e.to_string()),
        };
        if current != original {
            return Err("Claude settings changed concurrently; retry the audit".to_owned());
        }
        fs::rename(&temp, &target).map_err(|e| e.to_string())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result?;
    Ok("enabled (updated)")
}

/// Task-only entry point: never opens transcripts, evidence logs or task-supplied paths.
fn resume(args: Vec<String>) -> Result<(), String> {
    if args.len() > 1 { return Err("usage: omega resume [TASK]".into()); }
    if args.first().is_some_and(|s| s == "--help" || s == "-h") {
        println!("omega resume [TASK]\nRead active/ then completed/ task state before transcripts or logs.");
        return Ok(());
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is required")?);
    let root = home.join(".agents/tasks");
    let Some(task) = args.first() else {
        let mut tasks = Vec::new();
        for state in ["active", "completed"] {
            let dir = root.join(state);
            if !dir.exists() { continue; }
            for entry in fs::read_dir(dir).map_err(|e| e.to_string())? {
                let entry = entry.map_err(|e| e.to_string())?;
                let name = entry.file_name().to_string_lossy().into_owned();
                if entry.file_type().map_err(|e| e.to_string())?.is_file()
                    && name.strip_suffix(".md").is_some_and(|s| !s.is_empty() && s.len() <= 128
                        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')) {
                    let modified = entry.metadata().and_then(|m| m.modified())
                        .map_err(|e| e.to_string())?;
                    tasks.push((modified, state, name));
                }
            }
        }
        tasks.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| (&a.1, &a.2).cmp(&(&b.1, &b.2))));
        let total = tasks.len();
        tasks.truncate(20);
        let tasks: Vec<_> = tasks.into_iter().map(|(_, state, name)| (state, name)).collect();
        println!("{}", json!({"tasks": tasks, "total": total, "truncated": total > 20,
            "next": "Select the matching task with omega resume TASK; inspect Git and referenced evidence before transcripts."}));
        return Ok(());
    };
    let slug = task.strip_suffix(".md").unwrap_or(task);
    if slug.is_empty() || slug.len() > 128 || !slug.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Err("TASK must be a task basename (letters, digits, hyphens, underscores)".into());
    }
    for state in ["active", "completed"] {
        let path = root.join(state).join(format!("{slug}.md"));
        let mut file = match OpenOptions::new().read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC).open(&path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.to_string()),
        };
        if !file.metadata().map_err(|e| e.to_string())?.is_file() {
            return Err("Task must be a regular file".into());
        }
        let mut bytes = Vec::new();
        (&mut file).take(6001).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
        let mut truncated = bytes.len() > 6000;
        bytes.truncate(6000);
        loop {
            let result = json!({"task": path, "state": state,
                "content": String::from_utf8_lossy(&bytes), "truncated": truncated,
                "next": "Check current Git and task evidence; do not infer completion from state alone. Read transcripts only for a specific remaining gap."}).to_string();
            if result.len() <= 7600 {
                println!("{result}");
                break;
            }
            if bytes.is_empty() { return Err("Task path exceeds response budget".into()); }
            bytes.truncate(bytes.len() / 2);
            truncated = true;
        }
        return Ok(());
    }
    Err(format!("Task not found in active/ or completed/: {slug}"))
}

fn run() -> Result<(), String> {
    let mut args = std::env::args().skip(1).peekable();
    if args.peek().is_some_and(|a| a == "resume") {
        args.next();
        return resume(args.collect());
    }
    if args.peek().is_some_and(|a| a == "optimize") {
        args.next();
    }
    let mut apply = false;
    let mut limit = 12;
    let mut report = None;
    let mut json_output = false;
    let mut force = false;
    let mut check = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--apply" => apply = true,
            "--json" => json_output = true,
            "--force" => force = true,
            "--check" => check = true,
            "--sessions" => {
                limit = args
                    .next()
                    .ok_or("--sessions requires 1..50")?
                    .parse::<usize>()
                    .map_err(|_| "--sessions requires 1..50")?;
                if !(1..=50).contains(&limit) {
                    return Err("--sessions requires 1..50".to_owned());
                }
            }
            "--report" => {
                report = Some(PathBuf::from(
                    args.next()
                        .ok_or("--report requires a new JSON file path")?,
                ))
            }
            "--help" | "-h" => {
                println!(
                    "omega resume [TASK]\nRead bounded task state first; never opens transcripts or logs.\nomega optimize [--sessions 1..50] [--json] [--report NEW.json] [--apply | --check] [--force]\nAnalyze new Claude and Codex records since the last successful checkpoint.\nAlways saves a private report with findings and fix statuses before advancing checkpoints.\nDefault: enable the installed Claude Read guard when findings justify it.\n--check disables automatic fixes; --apply explicitly enables the guard.\n--force audits full sessions. Never deletes, rewrites or compacts conversations."
                );
                return Ok(());
            }
            _ => return Err(format!("Unknown argument: {arg}")),
        }
    }
    if check && apply {
        return Err("--check and --apply are mutually exclusive".to_owned());
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is required")?);
    let (_lock, state_path, mut state) = load_state(&home)?;
    let batch = sessions(&home, limit, &mut state, force)?;
    let report_path = match report {
        Some(path) => path,
        None => {
            let directory = home.join(".agents/tasks/reports/omega-optimize");
            fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
            directory.join(format!("{}-{}.json", timestamp()?, std::process::id()))
        }
    };
    // Reserve the report before changing configuration; never overwrite existing evidence.
    let mut report_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&report_path)
        .map_err(|e| e.to_string())?;
    let current_guard = configure(&home, false)?;
    let needs_guard = batch.rows.iter().any(|row| {
        row["provider"] == "claude" && row["large_unbounded_text_reads"].as_u64().unwrap_or(0) > 0
    });
    let auto_fix = !check && needs_guard && current_guard.starts_with("missing;");
    let guard = configure(&home, apply || auto_fix)?;
    let findings = ingest::findings(&batch.rows, guard);
    let output = json!({
        "snapshot_unix_seconds": timestamp()?,
        "selection": "most recently modified top-level transcripts; not proof of live sessions",
        "context_measure": "Claude: input + cache creation + cache read; Codex: last_token_usage.input_tokens (cached tokens already included); never byte estimates or cumulative cost",
        "read_guard": guard,
        "report_file": report_path,
        "findings": findings,
        "fixes_applied": if guard == "enabled (updated)" { 1 } else { 0 },
        "automatic_fixes": !check,
        "state_file": state_path,
        "sessions_skipped_unchanged": batch.unchanged,
        "sessions_changed_during_audit": batch.unstable,
        "sessions": batch.rows,
    });
    serde_json::to_writer_pretty(&mut report_file, &output).map_err(|e| e.to_string())?;
    report_file
        .write_all(b"\n")
        .and_then(|_| report_file.sync_all())
        .map_err(|e| e.to_string())?;
    state["last_report"] = json!(report_path);
    if json_output {
        println!("{output}");
        if batch.recorded > 0 {
            save_state(&state_path, &state)?;
        }
        return Ok(());
    }
    println!(
        "Claude + Codex incremental context analysis (not live-process detection)\nRead guard: {guard}\nPROVIDER SESSION   CONTEXT    PEAK       COMPACT  ERRORS/CALLS  LARGE READS/LOGS"
    );
    for row in output["sessions"].as_array().unwrap() {
        let id: String = row["session"]
            .as_str()
            .unwrap_or("?")
            .chars()
            .take(8)
            .collect();
        println!(
            "{:8} {id:8}  {:9}  {:9}  {:7}  {}/{}  {}/{}",
            row["provider"].as_str().unwrap_or("?"),
            row["context_last"],
            row["context_peak"],
            row["compactions"],
            row["tool_errors"],
            row["tool_calls"],
            row["large_unbounded_text_reads"],
            row["large_evidence_text_reads"]
        );
    }
    println!(
        "Unchanged skipped: {}; changed during audit (not marked): {}.\nPriorities: inspect evidence slices; bound code reads; checkpoint at phase changes.\nCounts indicate review targets, not proof of context rot. No transcript was modified.",
        batch.unchanged, batch.unstable
    );
    println!(
        "Report: {}\nFindings: {}; fixes applied: {}.",
        report_path.display(),
        output["findings"].as_array().unwrap().len(),
        output["fixes_applied"]
    );
    // A failed audit/configuration/report/output must not hide sessions on the next run.
    if batch.recorded > 0 {
        save_state(&state_path, &state)?;
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("omega optimize: {error}");
        std::process::exit(1);
    }
}
