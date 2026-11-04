//! Current branch of `cwd`, matching `git -C cwd branch --show-current` with a
//! `rev-parse --short HEAD` fallback. Plain repositories and worktrees are read
//! from `.git/HEAD`; every case git must decide (detached abbreviation length,
//! odd layouts, foreign ownership, GIT_* overrides) spawns git like the hook did.

use std::fs;
use std::io::{ErrorKind, Read};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::{py_strip, Abort};

const GIT_TIMEOUT: Duration = Duration::from_secs(2);
const GIT_OVERRIDES: [&str; 5] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
];

enum Head {
    Branch(String),
    Detached,
    NotRepo,
    Unknown,
}

pub fn branch(cwd: &str) -> Result<String, Abort> {
    if cwd.contains('\0') {
        return Err(Abort); // Python: ValueError from subprocess, not caught.
    }
    match read_head(cwd) {
        Head::Branch(b) => Ok(b),
        Head::NotRepo => Ok(String::new()),
        Head::Detached => via_git(cwd, false),
        Head::Unknown => via_git(cwd, true),
    }
}

fn via_git(cwd: &str, try_current: bool) -> Result<String, Abort> {
    if try_current {
        match run_git(cwd, &["branch", "--show-current"])? {
            None => return Ok(String::new()),
            Some(out) => {
                let out = py_strip(&out);
                if !out.is_empty() {
                    return Ok(out.to_owned());
                }
            }
        }
    }
    Ok(run_git(cwd, &["rev-parse", "--short", "HEAD"])?
        .map(|out| py_strip(&out).to_owned())
        .unwrap_or_default())
}

/// `Ok(None)` when git cannot run or times out (Python returns '' then).
fn run_git(cwd: &str, args: &[&str]) -> Result<Option<String>, Abort> {
    let Ok(mut child) = Command::new("git")
        .args(["-C", cwd])
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return Ok(None);
    };
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if start.elapsed() < GIT_TIMEOUT => std::thread::sleep(Duration::from_millis(1)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Ok(None);
            }
        }
    }
    let mut bytes = Vec::new();
    if let Some(mut out) = child.stdout.take() {
        let _ = out.read_to_end(&mut bytes);
    }
    String::from_utf8(bytes).map(Some).map_err(|_| Abort)
}

fn read_head(cwd: &str) -> Head {
    if GIT_OVERRIDES.iter().any(|k| std::env::var_os(k).is_some()) {
        return Head::Unknown;
    }
    let start = match fs::canonicalize(cwd) {
        Ok(p) => p,
        Err(e) if e.kind() == ErrorKind::NotFound => return Head::NotRepo,
        Err(_) => return Head::Unknown,
    };
    let Ok(meta) = fs::metadata(&start) else {
        return Head::Unknown;
    };
    if !meta.is_dir() {
        return Head::NotRepo;
    }
    // SAFETY: geteuid has no preconditions.
    let uid = unsafe { libc::geteuid() };
    let dev = meta.dev();
    let mut dir: &Path = &start;
    loop {
        match fs::symlink_metadata(dir.join(".git")) {
            Ok(m) => return head_of(dir, &m, uid),
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(_) => return Head::Unknown,
        }
        if dir.join("HEAD").exists() {
            return Head::Unknown; // bare-repository layout
        }
        let Some(parent) = dir.parent() else {
            return Head::NotRepo;
        };
        match fs::metadata(parent) {
            Ok(m) if m.dev() == dev => dir = parent,
            Ok(_) => return Head::NotRepo, // git does not cross filesystems
            Err(_) => return Head::Unknown,
        }
    }
}

fn owned(path: &Path, uid: u32) -> bool {
    fs::metadata(path).is_ok_and(|m| m.uid() == uid)
}

fn head_of(dir: &Path, dotgit: &fs::Metadata, uid: u32) -> Head {
    if !owned(dir, uid) {
        return Head::Unknown;
    }
    let gitdir: PathBuf = if dotgit.is_dir() {
        dir.join(".git")
    } else if dotgit.is_file() {
        let Ok(text) = fs::read_to_string(dir.join(".git")) else {
            return Head::Unknown;
        };
        let Some(path) = text.strip_prefix("gitdir: ") else {
            return Head::Unknown;
        };
        let path = path.trim_end_matches(['\n', '\r']);
        if path.is_empty() {
            return Head::Unknown;
        }
        dir.join(path) // an absolute path replaces the base
    } else {
        return Head::Unknown;
    };
    let head_file = gitdir.join("HEAD");
    let is_plain_file = fs::symlink_metadata(&head_file).is_ok_and(|m| m.is_file());
    let valid_layout = gitdir.join("commondir").is_file() || (gitdir.join("objects").is_dir() && gitdir.join("refs").is_dir());
    if !owned(&gitdir, uid) || !is_plain_file || !valid_layout {
        return Head::Unknown;
    }
    let Ok(text) = fs::read_to_string(&head_file) else {
        return Head::Unknown;
    };
    let line = text.strip_suffix('\n').unwrap_or(&text);
    if let Some(reference) = line.strip_prefix("ref: ") {
        return match reference.strip_prefix("refs/heads/") {
            Some(name) if !name.is_empty() && name != ".invalid" && !name.contains(char::is_whitespace) => {
                Head::Branch(name.to_owned())
            }
            _ => Head::Unknown,
        };
    }
    let is_oid = matches!(line.len(), 40 | 64) && line.bytes().all(|b| b.is_ascii_hexdigit());
    if is_oid { Head::Detached } else { Head::Unknown }
}
