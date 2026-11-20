//! Bounded, byte-addressed expansion of captured logs.

use crate::argparse::Parsed;
use crate::diag::read_line;
use crate::py::{self, Style};
use regex::bytes::{Regex, RegexBuilder};
use std::io::{BufReader, Seek, SeekFrom};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

const DEFAULT_LINES: i64 = 100;
const MAX_LINES: i64 = 200;
const DEFAULT_MAX_BYTES: i64 = 16000;
const MAX_BYTES: i64 = 30000;
/// Encoded-size cap of one result line (two labeled results share the budget).
const LINE_CAP: usize = 36000;
/// Longest line returned whole (6 bytes of JSON escaping each must fit half of LINE_CAP).
const LINE_LIMIT: usize = 2048;

/// A run of consecutive returned lines: `offset` locates its first byte, `end` follows its last.
struct Fragment {
    offset: u64,
    end: u64,
    text: Vec<u8>,
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
        py::json_str(&mut b, &f.text, Style::Py);
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
fn read_stream(directory: &[u8], stream: &str, offset: i64, lines: i64, max_bytes: i64, needle: Option<&Regex>, cap: usize, note: &str) -> Result<Vec<u8>, String> {
    let mut path = directory.to_vec();
    path.extend_from_slice(format!("/{}.log", stream).as_bytes());
    let open_error = |e: std::io::Error| py::oserror(e.raw_os_error().unwrap_or(0), &path);
    let mut file = std::fs::File::open(std::ffi::OsStr::from_bytes(&path)).map_err(open_error)?;
    let size = file.metadata().map_err(open_error)?.size();
    file.seek(SeekFrom::Start(offset as u64)).map_err(open_error)?;
    let mut source = BufReader::new(file);
    let (mut pos, mut next) = (offset as u64, offset as u64);
    let mut fragments: Vec<Fragment> = Vec::new();
    let mut used = 0i64;
    let mut raw = Vec::new();
    let mut count = 0i64;
    while count < lines && used < max_bytes {
        let at = pos;
        // A needle scan reads whole lines: budget-sized chunks would split a match.
        let limit = if needle.is_some() { LINE_LIMIT } else { LINE_LIMIT.min((max_bytes - used) as usize) };
        read_line(&mut source, &mut raw, limit).map_err(open_error)?;
        if raw.is_empty() {
            break;
        }
        pos += raw.len() as u64;
        let line = raw.strip_suffix(b"\n").unwrap_or(&raw);
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        if needle.is_none_or(|n| n.is_match(line)) {
            if count > 0 && used + raw.len() as i64 > max_bytes {
                pos = at;
                break;
            }
            // Contiguous lines share one fragment: per-line framing cost a third more tokens than the text.
            let joined = fragments.last().is_some_and(|last| last.end == at);
            match fragments.last_mut() {
                Some(last) if joined => last.text.extend_from_slice(&raw),
                _ => fragments.push(Fragment { offset: at, end: at, text: raw.clone() }),
            }
            if encode(directory, stream, offset, next, &fragments, false, note).len() > cap {
                let last = fragments.last_mut().expect("just extended");
                last.text.truncate(last.text.len() - raw.len());
                if !joined {
                    fragments.pop();
                }
                pos = at;
                break;
            }
            fragments.last_mut().expect("just extended").end = pos;
            count += 1;
            used += raw.len() as i64;
        }
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

/// Search pattern: a regex with smart case (case-insensitive unless the pattern has an
/// uppercase letter); text that is not a valid regex is searched literally. A valid regex also
/// matches its own literal text, so `error[E0308]` finds that rustc code.
fn matcher(pattern: &[u8]) -> Regex {
    let text = std::str::from_utf8(pattern).ok();
    let (mut escaped, mut upper) = (false, false);
    for c in text.unwrap_or_default().chars() {
        upper |= !escaped && c.is_uppercase();
        escaped = !escaped && c == '\\';
    }
    let build = |source: &str, fold: bool| RegexBuilder::new(source).case_insensitive(fold).build();
    let regex = text.filter(|t| build(t, !upper).is_ok());
    regex.and_then(|t| build(&format!("(?:{t})|{}", regex::escape(t)), !upper).ok()).unwrap_or_else(|| {
        let literal: String = pattern.iter().map(|b| format!("\\x{b:02x}")).collect();
        RegexBuilder::new(&literal).unicode(false).build().expect("escaped literal")
    })
}

/// Returns the JSON line, or the error text for the failure result.
/// Out-of-range `--lines`/`--max-bytes` are clamped (never exceeding the caps)
/// and reported in `note`. Stream `auto` reads stdout when stderr is empty or
/// absent, otherwise both streams, labeled, each with half the budget.
pub fn inspect(p: &Parsed) -> Result<Vec<u8>, String> {
    let offset = p.int("offset").unwrap_or(0);
    if offset < 0 {
        return Err("Require offset >= 0, lines 1..200, max-bytes 1..30000".into());
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
    let lines = clamp("--lines", p.int("lines"), DEFAULT_LINES, MAX_LINES);
    let max_bytes = clamp("--max-bytes", p.int("max_bytes"), DEFAULT_MAX_BYTES, MAX_BYTES);
    let needle = p.string("search");
    if let Some(n) = &needle
        && !(1..=512).contains(&n.len())
    {
        return Err("--search must contain 1..512 UTF-8 bytes".into());
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
    let needle = needle.as_deref().map(matcher);
    let needle = needle.as_ref();
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
