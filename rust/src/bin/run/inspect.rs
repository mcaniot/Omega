//! Bounded, byte-addressed expansion of captured logs.

use crate::argparse::Parsed;
use crate::diag::read_line;
use crate::py::{self, Style};
use std::io::{BufReader, Seek, SeekFrom};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;

struct Fragment {
    offset: u64,
    text: String,
}

fn encode(directory: &[u8], stream: &str, offset: i64, next: u64, fragments: &[Fragment], eof: bool) -> Vec<u8> {
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
    b.extend_from_slice(format!("],\"eof\":{}}}", eof).as_bytes());
    b
}

/// Returns the JSON line, or the error text for the failure result.
pub fn inspect(p: &Parsed) -> Result<Vec<u8>, String> {
    let offset = p.int("offset").unwrap_or(0);
    let lines = p.int("lines").unwrap_or(20);
    let max_bytes = p.int("max_bytes").unwrap_or(4096);
    if offset < 0 || !(1..=40).contains(&lines) || !(1..=7000).contains(&max_bytes) {
        return Err("Require offset >= 0, lines 1..40, max-bytes 1..7000".into());
    }
    let needle = p.string("search");
    if let Some(n) = &needle {
        if !(1..=512).contains(&n.len()) {
            return Err("--search must contain 1..512 UTF-8 bytes".into());
        }
    }
    let stream = String::from_utf8_lossy(&p.string("stream").unwrap_or_else(|| b"stderr".to_vec())).into_owned();
    let directory = py::resolve(&p.string("evidence").unwrap_or_default());
    let mut path = directory.clone();
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
        let found = needle.as_ref().and_then(|n| window.windows(n.len()).position(|w| w == n.as_slice()));
        if needle.is_none() || found.is_some() {
            let (fragment, fragment_offset) = match (&needle, found) {
                (Some(n), Some(m)) => {
                    let begin = m.saturating_sub(100);
                    (window[begin..(m + n.len() + 100).min(window.len())].to_vec(), at - overlap.len() as u64 + begin as u64)
                }
                _ => (raw.clone(), at),
            };
            fragments.push(Fragment { offset: fragment_offset, text: String::from_utf8_lossy(&fragment).into_owned() });
            if encode(&directory, &stream, offset, next, &fragments, false).len() > 7600 {
                fragments.pop();
                pos = at;
                break;
            }
            used += fragment.len() as i64;
        }
        overlap = match &needle {
            Some(n) if n.len() > 1 => window[window.len().saturating_sub(n.len() - 1)..].to_vec(),
            _ => Vec::new(),
        };
        next = pos;
    }
    Ok(encode(&directory, &stream, offset, next, &fragments, pos >= size))
}
