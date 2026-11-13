//! Per-session state under ~/.agents/state shared by the Claude hooks.
//! Every helper fails open: callers treat any error as "no state".
#![allow(dead_code)]
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

const MAX_ENTRIES: usize = 256;
const MAX_RANGES: usize = 64;
const MAX_STATE_BYTES: u64 = 1 << 20;

/// State file `~/.agents/state/<kind>/<session>.json`; None for unsafe session ids.
pub fn state_file(home: &Path, kind: &str, session: &str) -> Option<PathBuf> {
    let safe = !session.is_empty()
        && session.len() <= 128
        && session.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    safe.then(|| home.join(".agents/state").join(kind).join(format!("{session}.json")))
}

/// Atomic private write (dir 0700, file 0600) via temp file and rename.
pub fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let dir = path.parent().ok_or(std::io::ErrorKind::InvalidInput)?;
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    let tmp = dir.join(format!(".{}.{}.tmp", std::process::id(), path.file_name().and_then(|n| n.to_str()).unwrap_or("state")));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
        file.write_all(bytes)?;
        std::fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

pub fn read_bounded(path: &Path) -> Option<Vec<u8>> {
    if path.metadata().ok()?.len() > MAX_STATE_BYTES {
        return None;
    }
    std::fs::read(path).ok()
}

pub fn remove(path: &Path) {
    let _ = std::fs::remove_file(path);
}

#[derive(Default, Serialize, Deserialize)]
pub struct Entry {
    pub mtime_ns: u64,
    pub size: u64,
    /// Sorted, disjoint, inclusive 1-based line ranges.
    pub ranges: Vec<(u64, u64)>,
}

pub enum Coverage {
    Full,
    /// First uncovered inclusive gap of a partially covered request.
    Partial(u64, u64),
    Missing,
}

#[derive(Default, Serialize, Deserialize)]
pub struct Ledger(BTreeMap<String, Entry>);

/// (mtime_ns, size) identity of a file version.
pub fn stamp(meta: &std::fs::Metadata) -> (u64, u64) {
    (meta.mtime().max(0) as u64 * 1_000_000_000 + meta.mtime_nsec().max(0) as u64, meta.len())
}

/// Ledger key: canonical path, falling back to the given one.
pub fn key(path: &Path) -> String {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf()).to_string_lossy().into_owned()
}

impl Ledger {
    pub fn load(path: &Path) -> Self {
        read_bounded(path).and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
    }

    pub fn save(&self, path: &Path) {
        if let Ok(bytes) = serde_json::to_vec(self) {
            let _ = write_private(path, &bytes);
        }
    }

    pub fn forget(&mut self, key: &str) {
        self.0.remove(key);
    }

    fn entry(&mut self, key: &str, (mtime_ns, size): (u64, u64)) -> &mut Entry {
        if self.0.len() >= MAX_ENTRIES && !self.0.contains_key(key) {
            self.0.pop_first();
        }
        let entry = self.0.entry(key.to_string()).or_default();
        if entry.mtime_ns != mtime_ns || entry.size != size {
            *entry = Entry { mtime_ns, size, ranges: Vec::new() };
        }
        entry
    }

    pub fn check(&mut self, key: &str, stamp: (u64, u64), start: u64, end: u64) -> Coverage {
        let entry = self.entry(key, stamp);
        let (mut cursor, mut overlap, mut gap) = (start, false, None);
        for &(s, e) in &entry.ranges {
            if e < cursor {
                continue;
            }
            if s > end {
                break;
            }
            overlap = true;
            if s > cursor {
                gap = Some((cursor, s - 1));
                break;
            }
            cursor = e + 1;
            if cursor > end {
                break;
            }
        }
        if gap.is_none() && cursor <= end {
            gap = Some((cursor, end));
        }
        match (gap, overlap) {
            (None, _) => Coverage::Full,
            (Some((a, b)), true) => Coverage::Partial(a, b),
            (Some(_), false) => Coverage::Missing,
        }
    }

    pub fn record(&mut self, key: &str, stamp: (u64, u64), start: u64, end: u64) {
        let entry = self.entry(key, stamp);
        entry.ranges.push((start, end));
        entry.ranges.sort_unstable();
        let mut merged: Vec<(u64, u64)> = Vec::new();
        for &(s, e) in &entry.ranges {
            match merged.last_mut() {
                Some(last) if s <= last.1.saturating_add(1) => last.1 = last.1.max(e),
                _ => merged.push((s, e)),
            }
        }
        if merged.len() > MAX_RANGES {
            merged.remove(0);
        }
        entry.ranges = merged;
    }
}
