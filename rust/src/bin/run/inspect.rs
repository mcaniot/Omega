//! Bounded, byte-addressed expansion of captured logs.

use crate::argparse::Parsed;
use crate::diag::read_line;
use crate::py::{self, Style};
use std::io::{BufReader, Seek, SeekFrom};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

const DEFAULT_MAX_BYTES: i64 = 4096;
/// Encoded-size cap of one result line (two labeled results share the budget).
const LINE_CAP: usize = 7600;

struct Fragment {
    offset: u64,
    text: String,
}

fn encode(directory: &[u8], stream: &str, offset: i64, next: u64, fragments: &[Fragment], eof: bool, note: &str) -> Vec<u8> {
    let mut b = b"{\"evidence\":".to_vec();
    py::json_str(&mut b, directory, Style::Py);
    b.extend_from_slice(format!(",\"stream\":\"{}\",\"offset\":{},\"next_offset\":{},\"fragments\":[", stream, offset, next).as_bytes());
    for (i, f) in fragments.iter().enumerate() {
        if i > 0 {
            b.push(b',');
        }
        b.extend_from_slice(format!("{{\"offset\":{},\"text\":", f.offset).as_bytes());
        py::json_str(&mut b, f.text.as_bytes(), Style::Py);
        b.push(b'}');
    }
    b.extend_from_slice(format!("],\"eof\":{}", eof).as_bytes());
    if !note.is_empty() {
        b.extend_from_slice(b",\"note\":");
        py::json_str(&mut b, note.as_bytes(), Style::Py);
    }
    b.push(b'}');
    b
}

/// Reads one log of `directory`; `note` reports clamped inputs.
#[allow(clippy::too_many_arguments)]
fn read_stream(directory: &[u8], stream: &str, offset: i64, lines: i64, max_bytes: i64, needle: Option<&[u8]>, cap: usize, note: &str) -> Result<Vec<u8>, String> {
    let mut path = directory.to_vec();
    path.extend_from_slice(format!("/{}.log", stream).as_bytes());
    let open_error = |e: std::io::Error| py::oserror(e.raw_os_error().unwrap_or(0), &path);
    let mut file = std::fs::File::open(std::ffi::OsStr::from_bytes(&path)).map_err(open_error)?;
    let size = file.metadata().map_err(open_error)?.size();
    file.seek(SeekFrom::Start(offset as u64)).map_err(open_error)?;
    let mut source = BufReader::new(file);
    let (mut pos, mut next) = (offset as u64, offset as u64);
    let mut fragments: Vec<Fragment> = Vec::new();
    let (mut used, mut overlap) = (0i64, Vec::<u8>::new());
    let mut raw = Vec::new();
    while (fragments.len() as i64) < lines && used < max_bytes {
        let at = pos;
        read_line(&mut source, &mut raw, 1024.min((max_bytes - used) as usize)).map_err(open_error)?;
        if raw.is_empty() {
            break;
        }
        pos += raw.len() as u64;
        let mut window = overlap.clone();
        window.extend_from_slice(&raw);
        let found = needle.and_then(|n| window.windows(n.len()).position(|w| w == n));
        if needle.is_none() || found.is_some() {
            let (fragment, fragment_offset) = match (needle, found) {
                (Some(n), Some(m)) => {
                    let begin = m.saturating_sub(100);
                    (window[begin..(m + n.len() + 100).min(window.len())].to_vec(), at - overlap.len() as u64 + begin as u64)
                }
                _ => (raw.clone(), at),
            };
            fragments.push(Fragment { offset: fragment_offset, text: String::from_utf8_lossy(&fragment).into_owned() });
            if encode(directory, stream, offset, next, &fragments, false, note).len() > cap {
                fragments.pop();
                pos = at;
                break;
            }
            used += fragment.len() as i64;
        }
        overlap = match needle {
            Some(n) if n.len() > 1 => window[window.len().saturating_sub(n.len() - 1)..].to_vec(),
            _ => Vec::new(),
        };
        next = pos;
    }
    Ok(encode(directory, stream, offset, next, &fragments, pos >= size, note))
}

fn nonempty(directory: &[u8], stream: &str) -> bool {
    let path = Path::new(std::ffi::OsStr::from_bytes(directory)).join(format!("{}.log", stream));
    std::fs::metadata(path).is_ok_and(|m| m.len() > 0)
}

/// Splits an evidence argument into (directory, stream implied by a log path).
/// Accepts the evidence directory or `<dir>/{stdout,stderr}[.log]`.
fn locate(evidence: &[u8]) -> (Vec<u8>, Option<&'static str>) {
    let path = Path::new(std::ffi::OsStr::from_bytes(evidence));
    if !path.is_dir() {
        let implied = match path.file_name().map(|n| n.as_bytes()) {
            Some(b"stdout" | b"stdout.log") => Some("stdout"),
            Some(b"stderr" | b"stderr.log") => Some("stderr"),
            _ => None,
        };
        if let (Some(stream), Some(parent)) = (implied, path.parent()) {
            let parent = if parent.as_os_str().is_empty() { Path::new(".") } else { parent };
            return (py::resolve(parent.as_os_str().as_bytes()), Some(stream));
        }
    }
    (py::resolve(evidence), None)
}

/// Returns the JSON line, or the error text for the failure result.
/// Out-of-range `--lines`/`--max-bytes` are clamped (never exceeding the caps)
/// and reported in `note`. Stream `auto` reads stdout when stderr is empty or
/// absent, otherwise both streams, labeled, each with half the budget.
pub fn inspect(p: &Parsed) -> Result<Vec<u8>, String> {
    let offset = p.int("offset").unwrap_or(0);
    if offset < 0 {
        return Err("Require offset >= 0, lines 1..40, max-bytes 1..7000".into());
    }
    let mut notes: Vec<String> = Vec::new();
    let mut clamp = |name: &str, value: Option<i64>, default: i64, max: i64| -> i64 {
        let Some(v) = value else { return default };
        let c = v.clamp(1, max);
        if c != v {
            notes.push(format!("{} {} clamped to {}", name, v, c));
        }
        c
    };
    let lines = clamp("--lines", p.int("lines"), 20, 40);
    let max_bytes = clamp("--max-bytes", p.int("max_bytes"), DEFAULT_MAX_BYTES, 7000);
    let needle = p.string("search");
    if let Some(n) = &needle {
        if !(1..=512).contains(&n.len()) {
            return Err("--search must contain 1..512 UTF-8 bytes".into());
        }
    }
    let (directory, implied) = locate(&p.string("evidence").unwrap_or_default());
    let requested = String::from_utf8_lossy(&p.string("stream").unwrap_or_else(|| b"auto".to_vec())).into_owned();
    let stream = match (requested.strip_suffix(".log").unwrap_or(&requested), implied) {
        ("auto", Some(s)) => s,
        ("stdout", _) => "stdout",
        ("stderr", _) => "stderr",
        _ => "auto",
    };
    let note = notes.join("; ");
    let needle = needle.as_deref();
    if stream != "auto" {
        return read_stream(&directory, stream, offset, lines, max_bytes, needle, LINE_CAP, &note);
    }
    if !nonempty(&directory, "stderr") {
        return read_stream(&directory, "stdout", offset, lines, max_bytes, needle, LINE_CAP, &note);
    }
    let (half_lines, half_bytes) = ((lines / 2).max(1), (max_bytes / 2).max(1));
    let mut b = b"{\"evidence\":".to_vec();
    py::json_str(&mut b, &directory, Style::Py);
    b.extend_from_slice(b",\"stream\":\"both\",\"streams\":[");
    for (i, name) in ["stderr", "stdout"].into_iter().enumerate() {
        if i > 0 {
            b.push(b',');
        }
        b.extend_from_slice(&read_stream(&directory, name, offset, half_lines, half_bytes, needle, LINE_CAP / 2 - 100, "")?);
    }
    b.push(b']');
    if !note.is_empty() {
        b.extend_from_slice(b",\"note\":");
        py::json_str(&mut b, note.as_bytes(), Style::Py);
    }
    b.push(b'}');
    Ok(b)
}
