//! Read-only measurement of the fixed context paid by every request, per project: the minimum
//! first-request context of recent Claude sessions, the instruction chain (CLAUDE.md/AGENTS.md
//! files and `@imports`) and skill descriptions that explain it. Never loads/saves checkpoint
//! state and never touches settings; the only write is an optional new `--report` file.
use super::ingest;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

/// Rough bytes per token used to turn instruction/skill bytes into tokens (documented estimate).
const BYTES_PER_TOKEN: u64 = 4;
const WINDOW_DAYS: u64 = 30;
const INSTRUCTION_FINDING_BYTES: u64 = 8 * 1024;
const SKILLS_FINDING_BYTES: u64 = 4 * 1024;
const MAX_IMPORT_DEPTH: usize = 5;
const INSTRUCTION_NAMES: [&str; 5] = ["CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "AGENTS.local.md", ".claude/CLAUDE.md"];

#[derive(Default)]
struct Chain {
    files: Vec<Value>,
    duplicates: Vec<Value>,
    seen: HashSet<PathBuf>,
}

impl Chain {
    /// Load `path` and its `@path.md` imports; a realpath already loaded is a duplicate.
    fn load(&mut self, path: &Path, home: &Path, scope: &str, depth: usize) {
        let Ok(real) = fs::canonicalize(path) else { return };
        let Ok(text) = fs::read_to_string(&real) else { return };
        let bytes = text.len();
        if !self.seen.insert(real) {
            self.duplicates.push(json!({"path": path.display().to_string(), "bytes": bytes}));
            return;
        }
        self.files.push(json!({"path": path.display().to_string(), "bytes": bytes, "scope": scope}));
        if depth >= MAX_IMPORT_DEPTH {
            return;
        }
        let dir = path.parent().unwrap_or(Path::new("/"));
        let mut fenced = false;
        for line in text.lines() {
            if line.trim_start().starts_with("```") {
                fenced = !fenced;
            }
            if fenced {
                continue;
            }
            for token in line.split_whitespace().filter_map(|t| t.strip_prefix('@')) {
                let token = token.trim_end_matches([',', ';', ')', '.', '`', '"', '\'']);
                if !token.ends_with(".md") {
                    continue;
                }
                let target = match token.strip_prefix("~/") {
                    Some(rest) => home.join(rest),
                    None => dir.join(token),
                };
                self.load(&target, home, scope, depth + 1);
            }
        }
    }
}

fn description_bytes(skill: &Path) -> usize {
    let Ok(text) = fs::read_to_string(skill) else { return 0 };
    let mut lines = text.lines();
    if lines.next().map(str::trim) != Some("---") {
        return 0;
    }
    let mut value = String::new();
    let mut inside = false;
    for line in lines {
        if line.trim() == "---" {
            break;
        }
        if let Some(rest) = line.strip_prefix("description:") {
            inside = true;
            value.push_str(rest.trim());
        } else if inside && line.starts_with([' ', '\t']) {
            value.push(' ');
            value.push_str(line.trim());
        } else {
            inside = false;
        }
    }
    let value = value.trim().trim_matches(['"', '\'']).trim_start_matches(['>', '|', '-']).trim();
    value.len()
}

fn skills(dir: &Path) -> Value {
    let (mut count, mut bytes) = (0usize, 0usize);
    let mut skill_files: Vec<PathBuf> = fs::read_dir(dir).into_iter().flatten().flatten().map(|e| e.path().join("SKILL.md")).filter(|p| p.is_file()).collect();
    skill_files.sort();
    for file in &skill_files {
        count += 1;
        bytes += description_bytes(file);
    }
    json!({"count": count, "description_bytes": bytes})
}

#[derive(Default)]
struct Project {
    firsts: Vec<u64>,
    requests: u64,
    context: u64,
}

/// (cwd, first-request context, requests, total context) of one transcript, requests deduplicated by message id.
fn session(path: &Path) -> Result<Option<(String, u64, u64, u64)>, String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let (mut cwd, mut order, mut contexts): (Option<String>, Vec<String>, BTreeMap<String, u64>) = (None, Vec::new(), BTreeMap::new());
    for line in BufReader::new(file).lines() {
        let Ok(row) = serde_json::from_str::<Value>(&line.map_err(|e| e.to_string())?) else { continue };
        if cwd.is_none() {
            cwd = row["cwd"].as_str().map(str::to_string);
        }
        if row["type"] != "assistant" || row["isSidechain"] == true {
            continue;
        }
        let usage = &row["message"]["usage"];
        if !usage.is_object() {
            continue;
        }
        let id = row["message"]["id"].as_str().map(str::to_string).unwrap_or_else(|| format!("#{}", order.len()));
        let context = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"].iter().map(|k| usage[k].as_u64().unwrap_or(0)).sum();
        if contexts.insert(id.clone(), context).is_none() {
            order.push(id);
        }
    }
    let Some(cwd) = cwd else { return Ok(None) };
    // Synthetic/aborted messages report zero usage; the first real request defines the prefix.
    let Some(first) = order.iter().map(|id| contexts[id]).find(|c| *c > 0) else { return Ok(None) };
    Ok(Some((cwd, first, order.len() as u64, contexts.values().sum())))
}

fn median(mut values: Vec<u64>) -> f64 {
    values.sort_unstable();
    match values.len() {
        0 => 0.0,
        n if n % 2 == 1 => values[n / 2] as f64,
        n => (values[n / 2 - 1] + values[n / 2]) as f64 / 2.0,
    }
}

fn instruction_chain(cwd: &Path, home: &Path, global: &Chain) -> Chain {
    let mut chain = Chain { seen: global.seen.clone(), ..Chain::default() };
    let global_claude = home.join(".claude/CLAUDE.md");
    let mut dirs = vec![cwd.to_path_buf()];
    if cwd.starts_with(home) {
        dirs = cwd.ancestors().take_while(|d| d.starts_with(home)).map(Path::to_path_buf).collect();
    }
    for dir in dirs {
        for name in INSTRUCTION_NAMES {
            let path = dir.join(name);
            if path != global_claude {
                chain.load(&path, home, "project", 0);
            }
        }
    }
    chain
}

fn sum(files: &[Value]) -> u64 {
    files.iter().filter_map(|f| f["bytes"].as_u64()).sum()
}

pub fn run(args: Vec<String>) -> Result<(), String> {
    let (mut json_output, mut report) = (false, None);
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--json" => json_output = true,
            "--report" => report = Some(PathBuf::from(args.next().ok_or("--report requires a new JSON file path")?)),
            _ => return Err(format!("Unknown argument: {arg}")),
        }
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME is required")?);
    let cutoff = SystemTime::now() - Duration::from_secs(WINDOW_DAYS * 86_400);
    let mut paths = Vec::new();
    if let Ok(root) = fs::read_dir(home.join(".claude/projects")) {
        for directory in root.flatten() {
            let name = directory.file_name().to_string_lossy().to_lowercase();
            if !name.contains("bench") && directory.file_type().map_err(|e| e.to_string())?.is_dir() {
                ingest::discover(&directory.path(), false, &mut paths)?;
            }
        }
    }
    paths.sort();
    let mut projects: BTreeMap<String, Project> = BTreeMap::new();
    for path in paths {
        if path.metadata().and_then(|m| m.modified()).map_err(|e| e.to_string())? < cutoff {
            continue;
        }
        if let Some((cwd, first, requests, context)) = session(&path)? {
            let project = projects.entry(cwd).or_default();
            project.firsts.push(first);
            project.requests += requests;
            project.context += context;
        }
    }
    let mut global = Chain::default();
    global.load(&home.join(".claude/CLAUDE.md"), &home, "global", 0);
    let global_skills = skills(&home.join(".claude/skills"));
    let (mut rows, mut findings) = (Vec::new(), Vec::new());
    let (mut prefix_cost, mut context_total, mut floor) = (0u64, 0u64, u64::MAX);
    for (cwd, project) in &projects {
        let chain = instruction_chain(Path::new(cwd), &home, &global);
        let prefix = *project.firsts.iter().min().unwrap_or(&0);
        let project_bytes = sum(&chain.files);
        let duplicate_bytes = sum(&chain.duplicates);
        let project_skills = skills(&Path::new(cwd).join(".claude/skills"));
        let skill_bytes = project_skills["description_bytes"].as_u64().unwrap_or(0);
        let reducible = project_bytes + skill_bytes + duplicate_bytes;
        let cost = prefix * project.requests;
        let share = if project.context > 0 { cost as f64 / project.context as f64 } else { 0.0 };
        prefix_cost += cost;
        context_total += project.context;
        floor = floor.min(prefix);
        let tokens = reducible / BYTES_PER_TOKEN;
        let mut files = global.files.clone();
        files.extend(chain.files.iter().cloned());
        for dup in &chain.duplicates {
            findings.push(json!({"project": cwd, "kind": "duplicate_instruction_file", "path": dup["path"], "bytes": dup["bytes"]}));
        }
        if project_bytes > INSTRUCTION_FINDING_BYTES {
            findings.push(json!({"project": cwd, "kind": "instruction_chain_over_8kib", "bytes": project_bytes}));
        }
        if skill_bytes > SKILLS_FINDING_BYTES {
            findings.push(json!({"project": cwd, "kind": "skills_over_4kib", "bytes": skill_bytes}));
        }
        rows.push(json!({
            "cwd": cwd, "sessions": project.firsts.len(), "requests": project.requests,
            "first_context_min": prefix, "first_context_median": median(project.firsts.clone()),
            "context_total": project.context, "prefix_share": share,
            "instruction_files": files, "instruction_bytes": sum(&files),
            "project_instruction_bytes": project_bytes,
            "duplicates": chain.duplicates, "duplicate_bytes": duplicate_bytes,
            "skills": project_skills,
            "reducible_bytes": reducible, "reducible_tokens_per_request": tokens,
            "reducible_tokens_total": tokens * project.requests,
        }));
    }
    rows.sort_by(|a, b| b["reducible_tokens_total"].as_u64().cmp(&a["reducible_tokens_total"].as_u64()).then_with(|| a["cwd"].as_str().cmp(&b["cwd"].as_str())));
    let output = json!({
        "model": {"bytes_per_token": BYTES_PER_TOKEN, "window_days": WINDOW_DAYS,
                  "instruction_finding_bytes": INSTRUCTION_FINDING_BYTES, "skills_finding_bytes": SKILLS_FINDING_BYTES},
        "global": {"instruction_files": global.files, "instruction_bytes": sum(&global.files), "skills": global_skills},
        "projects": rows,
        "findings": findings,
        "aggregate": {
            "projects": projects.len(),
            "global_floor_tokens": if projects.is_empty() { 0 } else { floor },
            "weighted_prefix_share": if context_total > 0 { prefix_cost as f64 / context_total as f64 } else { 0.0 },
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
    println!("SESS  REQS    MIN_PREFIX  SHARE   INSTR_B  SKILL_B  REDUC_TOK/REQ  PROJECT");
    for row in output["projects"].as_array().into_iter().flatten() {
        println!("{:<4}  {:<6}  {:<10}  {:<5.1}%  {:<7}  {:<7}  {:<13}  {}", row["sessions"], row["requests"], row["first_context_min"],
            row["prefix_share"].as_f64().unwrap_or(0.0) * 100.0, row["instruction_bytes"], row["skills"]["description_bytes"],
            row["reducible_tokens_per_request"], row["cwd"].as_str().unwrap_or("?"));
    }
    for f in &findings {
        println!("FINDING {} {} {}", f["kind"].as_str().unwrap_or("?"), f["project"].as_str().unwrap_or("?"), f["path"].as_str().unwrap_or(&f["bytes"].to_string()));
    }
    let a = &output["aggregate"];
    println!("Global floor {} tokens; weighted prefix share {:.1}%; global instructions {} bytes, global skills {} bytes",
        a["global_floor_tokens"], a["weighted_prefix_share"].as_f64().unwrap_or(0.0) * 100.0,
        output["global"]["instruction_bytes"], output["global"]["skills"]["description_bytes"]);
    Ok(())
}
