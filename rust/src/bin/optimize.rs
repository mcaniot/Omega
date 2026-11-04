//! Repeatable local context audit; never rewrites or compacts conversations.
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

fn audit(path: &Path) -> Result<Value, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut messages = HashMap::new();
    let mut calls = HashMap::new();
    let mut results = HashMap::new();
    let mut compactions = HashSet::new();
    let mut malformed = 0;
    let mut project = String::new();
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|e| e.to_string())?;
        let row: Value = match serde_json::from_str(&line) {
            Ok(row) => row,
            Err(_) => {
                malformed += 1;
                continue;
            }
        };
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
    for (id, call) in &calls {
        let signature = format!("{}:{}", call["name"], call["input"]);
        if !signatures.insert(signature) {
            repeated += 1;
        }
        if call["name"] != "Read" {
            continue;
        }
        let Some(result) = results.get(id) else {
            continue;
        };
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
            if call["input"].get("limit").is_none() {
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
    Ok(json!({
        "session": path.file_stem().and_then(|p| p.to_str()).unwrap_or("?"),
        "project": project,
        "last_assistant_utc": ordered.last().map(|v| &v.2),
        "requests": ordered.len(),
        "context_first": ordered.first().map(|v| v.1).unwrap_or(0),
        "context_last": ordered.last().map(|v| v.1).unwrap_or(0),
        "context_peak": ordered.iter().map(|v| v.1).max().unwrap_or(0),
        "compactions": compactions.len(),
        "tool_calls": calls.len(),
        "tool_errors": results.values().filter(|r| r["is_error"] == true).count(),
        "repeated_exact_calls_extra": repeated,
        "large_unbounded_text_reads": large_unbounded_reads,
        "large_evidence_text_reads": large_evidence_reads,
        "malformed_records": malformed,
    }))
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
    let root = home.join(".claude/projects");
    if !root.exists() {
        return Ok(batch);
    }
    let mut files = Vec::new();
    for directory in fs::read_dir(&root).map_err(|e| e.to_string())? {
        let directory = directory.map_err(|e| e.to_string())?;
        if !directory.file_type().map_err(|e| e.to_string())?.is_dir() {
            continue;
        }
        for entry in fs::read_dir(directory.path()).map_err(|e| e.to_string())? {
            let path = entry.map_err(|e| e.to_string())?.path();
            if path.extension().is_some_and(|ext| ext == "jsonl") {
                let modified = path
                    .metadata()
                    .and_then(|m| m.modified())
                    .map_err(|e| e.to_string())?;
                files.push((modified, path));
            }
        }
    }
    files.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    for (_, path) in files.into_iter().take(limit) {
        let key = path
            .strip_prefix(&root)
            .map_err(|e| e.to_string())?
            .to_string_lossy()
            .into_owned();
        let before = fingerprint(&path)?;
        if !force && state["sessions"][&key]["fingerprint"] == before {
            batch.unchanged += 1;
            continue;
        }
        batch.rows.push(audit(&path)?);
        if fingerprint(&path)? == before {
            state["sessions"][&key] =
                json!({"fingerprint": before, "last_analyzed_unix_seconds": timestamp()?});
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

fn run() -> Result<(), String> {
    let mut args = std::env::args().skip(1).peekable();
    if args.peek().is_some_and(|a| a == "optimize") {
        args.next();
    }
    let mut apply = false;
    let mut limit = 12;
    let mut report = None;
    let mut json_output = false;
    let mut force = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--apply" => apply = true,
            "--json" => json_output = true,
            "--force" => force = true,
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
                    "omega optimize [--sessions 1..50] [--json] [--report NEW.json] [--apply] [--force]\nAudit changed local Claude transcripts; --force includes unchanged sessions.\nRecords successful analyses in ~/.agents/tasks/state/omega-optimize.json.\n--apply only enables the installed Read guard. Never deletes or compacts sessions."
                );
                return Ok(());
            }
            _ => return Err(format!("Unknown argument: {arg}")),
        }
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is required")?);
    let (_lock, state_path, mut state) = load_state(&home)?;
    let batch = sessions(&home, limit, &mut state, force)?;
    let guard = configure(&home, apply)?;
    let output = json!({
        "snapshot_unix_seconds": timestamp()?,
        "selection": "most recently modified top-level transcripts; not proof of live sessions",
        "context_measure": "input + cache creation + cache read tokens, not cumulative cost or window utilization",
        "read_guard": guard,
        "state_file": state_path,
        "sessions_skipped_unchanged": batch.unchanged,
        "sessions_changed_during_audit": batch.unstable,
        "sessions": batch.rows,
    });
    if let Some(path) = report {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(|e| e.to_string())?;
        serde_json::to_writer_pretty(&mut file, &output).map_err(|e| e.to_string())?;
        writeln!(file).map_err(|e| e.to_string())?;
    }
    if json_output {
        println!("{output}");
        if batch.recorded > 0 {
            save_state(&state_path, &state)?;
        }
        return Ok(());
    }
    println!(
        "Claude context audit (transcript recency, not live-process detection)\nRead guard: {guard}\nSESSION   CONTEXT    PEAK       COMPACT  ERRORS/CALLS  LARGE READS/LOGS"
    );
    for row in output["sessions"].as_array().unwrap() {
        let id: String = row["session"]
            .as_str()
            .unwrap_or("?")
            .chars()
            .take(8)
            .collect();
        println!(
            "{id:8}  {:9}  {:9}  {:7}  {}/{}  {}/{}",
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
