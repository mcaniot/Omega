//! Result summary: inline output and ranked diagnostics with separate JSON budgets.

use crate::py::{self, Style};
use regex::Regex;
use std::collections::{HashSet, VecDeque};
use std::fs::File;
use std::io::BufRead;

const EDGE: usize = 4;
const LIMIT: usize = 8;
const BUDGET: usize = 4000;
// Allow 8 KiB of output plus metadata and JSON escaping; large expansions fall back to diagnostics.
pub const INLINE_BUDGET: usize = 12 * 1024;

#[derive(Clone)]
pub struct Item {
    pub stream: &'static str,
    pub line: u64,
    pub offset: u64,
    pub text: String,
}

pub struct Summary {
    pub status: String,
    pub exit_code: i32,
    pub bytes: (i64, i64),
    pub signal: Option<String>,
    pub evidence: Vec<u8>,
    /// Why the status differs from the raw exit code; omitted when empty.
    pub note: String,
    pub stdout: Option<String>,
    pub stderr: Option<String>,
    pub diagnostics: Vec<Item>,
    pub preview: Vec<Item>,
}

impl Summary {
    pub fn encode(&self, style: Style) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(b"{\"status\":");
        py::json_str(&mut b, self.status.as_bytes(), style);
        b.extend_from_slice(format!(",\"exit_code\":{}", self.exit_code).as_bytes());
        // Sizes matter only when output exists but was not inlined.
        if self.stdout.is_none() && self.stderr.is_none() && self.bytes.0 + self.bytes.1 > 0 {
            b.extend_from_slice(format!(",\"bytes\":{{\"stdout\":{},\"stderr\":{}}}", self.bytes.0, self.bytes.1).as_bytes());
        }
        // The native writer emits evidence before signal; the Python path the reverse.
        let evidence = |b: &mut Vec<u8>| {
            b.extend_from_slice(b",\"evidence\":");
            py::json_str(b, &self.evidence, style);
        };
        let signal = |b: &mut Vec<u8>| {
            if let Some(sig) = &self.signal {
                b.extend_from_slice(b",\"signal\":");
                py::json_str(b, sig.as_bytes(), style);
            }
        };
        if style == Style::C {
            evidence(&mut b);
            signal(&mut b);
        } else {
            signal(&mut b);
            evidence(&mut b);
        }
        for (key, value) in [("stdout", &self.stdout), ("stderr", &self.stderr)] {
            if let Some(v) = value {
                b.extend_from_slice(format!(",\"{}\":", key).as_bytes());
                py::json_str(&mut b, v.as_bytes(), style);
            }
        }
        if !self.note.is_empty() {
            b.extend_from_slice(b",\"note\":");
            py::json_str(&mut b, self.note.as_bytes(), style);
        }
        for (key, items) in [("diagnostics", &self.diagnostics), ("preview", &self.preview)] {
        if items.is_empty() { continue; }
        b.extend_from_slice(format!(",\"{key}\":[").as_bytes());
        for (i, item) in items.iter().enumerate() {
            if i > 0 {
                b.push(b',');
            }
            b.extend_from_slice(format!("{{\"stream\":\"{}\",\"line\":{},\"offset\":{},\"text\":", item.stream, item.line, item.offset).as_bytes());
            py::json_str(&mut b, item.text.as_bytes(), style);
            b.push(b'}');
        }
        b.push(b']');
        }
        b.push(b'}');
        b
    }
}

struct Patterns {
    tiers: [Regex; 3],
    passed: Regex,
    ansi: Regex,
    unprintable: Regex,
}

fn patterns() -> Patterns {
    let compile = |p: &str| Regex::new(p).expect("static pattern");
    Patterns {
        tiers: [
            compile(
                r#"Traceback|\b[A-Z]\w*(?:Error|Exception)\b|^E\s|^(?:FAILED|ERROR|FATAL)\b|(?i:\[(?:error|fatal)\])|\berror:|[Ss]egmentation fault|core dumped|\bpanicked\b|terminate called|\bwhat\(\):|Assertion .* failed|\bAborted\b"#,
            ),
            compile(r"(?i)\b(?:errors?|exception|fatal|failed|failure|abort(?:ed)?)\b"),
            compile(r#"\b[^\s:]+\.(?:py|c|cc|cpp|cu|h|hpp):\d+|File ".+", line \d+"#),
        ],
        passed: compile(r"\s(?:PASSED|SKIPPED|XFAIL)\b|\[\s*OK\s*\]"),
        ansi: compile(r"\x1b\[[0-?]*[ -/]*[@-~]"),
        unprintable: compile(r"[\p{C}\p{Z}--[ ]]"),
    }
}

/// Removes ANSI escapes from lossily decoded output.
pub fn strip_ansi(bytes: &[u8]) -> String {
    let p = patterns();
    p.ansi.replace_all(&String::from_utf8_lossy(bytes), "").into_owned()
}

#[derive(Default)]
struct Edges {
    first: Vec<Item>,
    last: VecDeque<Item>,
    seen: HashSet<String>,
}

impl Edges {
    fn add(&mut self, item: &Item) {
        if !self.seen.insert(item.text.clone()) {
            return;
        }
        if self.first.len() < EDGE {
            self.first.push(item.clone());
        } else {
            if self.last.len() == EDGE {
                self.last.pop_front();
            }
            self.last.push_back(item.clone());
        }
        if self.seen.len() > 256 {
            self.seen = self.first.iter().chain(self.last.iter()).map(|i| i.text.clone()).collect();
        }
    }

    fn items(&self) -> impl Iterator<Item = &Item> {
        self.first.iter().chain(self.last.iter())
    }
}

/// `readline(limit)`: up to `limit` bytes, ending after a newline.
pub fn read_line(reader: &mut impl BufRead, buf: &mut Vec<u8>, limit: usize) -> std::io::Result<()> {
    buf.clear();
    while buf.len() < limit {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            break;
        }
        let take = (limit - buf.len()).min(available.len());
        match available[..take].iter().position(|&b| b == b'\n') {
            Some(i) => {
                buf.extend_from_slice(&available[..=i]);
                reader.consume(i + 1);
                break;
            }
            None => {
                buf.extend_from_slice(&available[..take]);
                reader.consume(take);
            }
        }
    }
    Ok(())
}

/// Diagnostics ordered by importance (callers drop from the end). Streams are
/// read through pinned descriptors, never through mutable evidence names.
fn excerpts(stdout_fd: i32, stderr_fd: i32, preview: bool) -> (Vec<Item>, Vec<Item>) {
    let p = patterns();
    let mut tiers: [Edges; 3] = Default::default();
    let mut tail: VecDeque<Item> = VecDeque::new();
    let mut boundaries = Vec::new();
    let mut buf = Vec::new();
    for (stream, fd) in [("stdout", stdout_fd), ("stderr", stderr_fd)] {
        let Ok(file) = File::open(format!("/proc/self/fd/{}", fd)) else { continue };
        let mut reader = std::io::BufReader::with_capacity(65536, file);
        let (mut line, mut offset) = (1u64, 0u64);
        let (mut first, mut last) = (None, None);
        loop {
            if read_line(&mut reader, &mut buf, 8192).is_err() || buf.is_empty() {
                break;
            }
            let (this_line, this_offset) = (line, offset);
            offset += buf.len() as u64;
            line += u64::from(buf.ends_with(b"\n"));
            let decoded = String::from_utf8_lossy(&buf);
            let stripped = p.ansi.replace_all(&decoded, "");
            let trimmed = stripped.trim_matches(py::py_space);
            let clean = p.unprintable.replace_all(trimmed, " ");
            let text: String = clean.chars().take(220).collect();
            if text.is_empty() {
                continue;
            }
            let item = Item { stream, line: this_line, offset: this_offset, text };
            if tail.len() == 3 {
                tail.pop_front();
            }
            tail.push_back(item.clone());
            first.get_or_insert_with(|| item.clone());
            let mut end: Vec<char> = clean.chars().rev().take(220).collect();
            end.reverse();
            last = Some(Item { text: end.into_iter().collect(), ..item.clone() });
            if p.passed.is_match(&item.text) {
                continue;
            }
            for (pattern, edges) in p.tiers.iter().zip(tiers.iter_mut()) {
                if pattern.is_match(&item.text) {
                    edges.add(&item);
                    break;
                }
            }
        }
        if let Some(item) = first {
            boundaries.push(item);
        }
        if let Some(item) = last {
            boundaries.push(item);
        }
    }
    let selected: Vec<Item> = tiers.iter().flat_map(|e| e.items().cloned()).take(LIMIT).collect();
    let mut snippets = Vec::new();
    if preview {
        snippets.extend(selected.iter().take(4).cloned());
        for item in boundaries {
            if !snippets.iter().any(|old| old.stream == item.stream && old.offset == item.offset && old.text == item.text) {
                snippets.push(item);
            }
        }
    }
    (if selected.is_empty() { tail.into_iter().collect() } else { selected }, snippets)
}

pub fn read_fd(fd: i32) -> Vec<u8> {
    let mut data = Vec::new();
    let mut buf = vec![0u8; 65536];
    loop {
        let n = unsafe { libc::pread(fd, buf.as_mut_ptr().cast(), buf.len(), data.len() as libc::off_t) };
        if n < 0 && py::errno() == libc::EINTR {
            continue;
        }
        if n <= 0 {
            return data;
        }
        data.extend_from_slice(&buf[..n as usize]);
    }
}

/// Complete Python-style result: used whenever the native fast path cannot
/// represent the output (non-ASCII, ANSI, NUL, failures with large output,
/// JSON escaping over budget).
pub fn finalize(mut s: Summary, inline_bytes: i64, stdout_fd: i32, stderr_fd: i32) -> Summary {
    let encoded = |s: &Summary| s.encode(Style::Py).len();
    let mut inlined = false;
    if s.bytes.0 + s.bytes.1 <= inline_bytes {
        let out = strip_ansi(&read_fd(stdout_fd));
        let err = strip_ansi(&read_fd(stderr_fd));
        s.stdout = (!out.is_empty()).then_some(out);
        s.stderr = (!err.is_empty()).then_some(err);
        s.diagnostics = Vec::new();
        inlined = true;
    }
    if encoded(&s) > INLINE_BUDGET || !inlined {
        s.stdout = None;
        s.stderr = None;
        if s.exit_code != 0 || inline_bytes > 0 {
            let (diagnostics, mut preview) = excerpts(stdout_fd, stderr_fd, inline_bytes > 0);
            if s.exit_code != 0 {
                preview.retain(|p| !diagnostics.iter().any(|d| d.stream == p.stream && d.offset == p.offset && d.text == p.text));
                s.diagnostics = diagnostics;
            }
            s.preview = preview;
        }
    }
    while encoded(&s) > BUDGET && !s.preview.is_empty() {
        s.preview.pop();
    }
    while encoded(&s) > BUDGET && !s.diagnostics.is_empty() {
        s.diagnostics.pop();
    }
    s.diagnostics.sort_by(|a, b| (a.stream, a.line).cmp(&(b.stream, b.line)));
    s.preview.sort_by(|a, b| (a.stream, a.line).cmp(&(b.stream, b.line)));
    s
}
