//! Helpers reproducing the Python behaviours the executor's output contract was
//! defined with: JSON encoding, `repr`, `OSError` text, `Path.resolve()` and
//! `int()`/`float()` parsing.

use std::ffi::{CStr, CString};
use std::os::unix::ffi::OsStrExt;

#[derive(Clone, Copy, PartialEq)]
pub enum Style {
    /// Native compact encoder: no spaces; other control bytes are `\u00xx`.
    C,
    /// `json.dumps(..., ensure_ascii=False)`.
    Py,
    /// `json.dumps(...)` default: non-ASCII escaped.
    PyAscii,
}

pub fn json_str(out: &mut Vec<u8>, s: &[u8], style: Style) {
    out.push(b'"');
    if style == Style::PyAscii {
        for c in String::from_utf8_lossy(s).chars() {
            escape_char(out, c);
        }
    } else {
        for &b in s {
            escape_byte(out, b);
        }
    }
    out.push(b'"');
}

fn escape_byte(out: &mut Vec<u8>, b: u8) {
    match b {
        b'"' => out.extend_from_slice(b"\\\""),
        b'\\' => out.extend_from_slice(b"\\\\"),
        // Short escapes in every style: `\u000a` costs three times `\n` in model context.
        b'\n' => out.extend_from_slice(b"\\n"),
        b'\r' => out.extend_from_slice(b"\\r"),
        b'\t' => out.extend_from_slice(b"\\t"),
        8 => out.extend_from_slice(b"\\b"),
        12 => out.extend_from_slice(b"\\f"),
        0..=31 => out.extend_from_slice(format!("\\u{:04x}", b).as_bytes()),
        _ => out.push(b),
    }
}

fn escape_char(out: &mut Vec<u8>, c: char) {
    if c.is_ascii() {
        escape_byte(out, c as u8);
    } else {
        let mut units = [0u16; 2];
        for unit in c.encode_utf16(&mut units) {
            out.extend_from_slice(format!("\\u{:04x}", unit).as_bytes());
        }
    }
}

pub fn strerror(errno: i32) -> String {
    unsafe { CStr::from_ptr(libc::strerror(errno)) }
        .to_string_lossy()
        .into_owned()
}

pub fn errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// Python `repr()` of a `str` decoded with surrogateescape.
pub fn repr(s: &[u8]) -> String {
    let quote = if s.contains(&b'\'') && !s.contains(&b'"') { '"' } else { '\'' };
    let mut out = String::new();
    out.push(quote);
    let mut rest = s;
    while !rest.is_empty() {
        let (valid, bad) = match std::str::from_utf8(rest) {
            Ok(v) => (v, 0),
            Err(e) => {
                let n = e.valid_up_to();
                (std::str::from_utf8(&rest[..n]).unwrap(), e.error_len().unwrap_or(rest.len() - n))
            }
        };
        for c in valid.chars() {
            match c {
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if c == quote => {
                    out.push('\\');
                    out.push(c);
                }
                c if (c as u32) < 32 || c as u32 == 127 => out.push_str(&format!("\\x{:02x}", c as u32)),
                c => out.push(c),
            }
        }
        for b in &rest[valid.len()..valid.len() + bad] {
            out.push_str(&format!("\\udc{:02x}", b));
        }
        rest = &rest[valid.len() + bad..];
    }
    out.push(quote);
    out
}

/// `str(OSError(errno, strerror, filename))`.
pub fn oserror(errno: i32, filename: &[u8]) -> String {
    format!("[Errno {}] {}: {}", errno, strerror(errno), repr(filename))
}

pub fn cstring(bytes: &[u8]) -> CString {
    CString::new(bytes).unwrap_or_else(|_| CString::new(Vec::new()).unwrap())
}

/// `os.path.realpath(path, strict=False)` after making `path` absolute.
pub fn resolve(path: &[u8]) -> Vec<u8> {
    let mut pending: Vec<Vec<u8>> = Vec::new();
    let mut result: Vec<u8>;
    let path = if path.is_empty() { b".".as_slice() } else { path };
    if path[0] == b'/' {
        result = Vec::new();
    } else {
        result = std::env::current_dir()
            .map(|d| d.as_os_str().as_bytes().to_vec())
            .unwrap_or_default();
        if result == b"/" {
            result.clear();
        }
    }
    let push_components = |pending: &mut Vec<Vec<u8>>, p: &[u8]| {
        for part in p.rsplit(|&b| b == b'/') {
            pending.push(part.to_vec());
        }
    };
    push_components(&mut pending, path);
    let mut hops = 0;
    while let Some(name) = pending.pop() {
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if let Some(i) = result.iter().rposition(|&b| b == b'/') {
                result.truncate(i);
            }
            continue;
        }
        let mut next = result.clone();
        next.push(b'/');
        next.extend_from_slice(&name);
        match std::fs::symlink_metadata(std::ffi::OsStr::from_bytes(&next)) {
            Ok(m) if m.file_type().is_symlink() => {
                hops += 1;
                match std::fs::read_link(std::ffi::OsStr::from_bytes(&next)) {
                    Ok(target) if hops <= 40 => {
                        let target = target.as_os_str().as_bytes().to_vec();
                        if target.first() == Some(&b'/') {
                            result.clear();
                        }
                        push_components(&mut pending, &target);
                    }
                    _ => {
                        result = next;
                        while let Some(rest) = pending.pop() {
                            if !rest.is_empty() {
                                result.push(b'/');
                                result.extend_from_slice(&rest);
                            }
                        }
                    }
                }
            }
            _ => result = next,
        }
    }
    if result.is_empty() {
        result.push(b'/');
    }
    result
}

fn digits(s: &str) -> Option<String> {
    // Python permits single underscores between digits.
    if s.is_empty() || s.starts_with('_') || s.ends_with('_') || s.contains("__") {
        return None;
    }
    Some(s.replace('_', ""))
}

/// `int(text)` clamped to the i64 range.
pub fn parse_int(text: &[u8]) -> Option<i64> {
    let text = std::str::from_utf8(text).ok()?.trim_matches(py_space);
    let (negative, body) = match text.as_bytes().first()? {
        b'-' => (true, &text[1..]),
        b'+' => (false, &text[1..]),
        _ => (false, text),
    };
    if !body.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    let value: i128 = match digits(body)?.parse::<i128>() {
        Ok(v) => v,
        Err(_) => i128::MAX,
    };
    let value = if negative { -value } else { value };
    Some(value.clamp(i64::MIN as i128, i64::MAX as i128) as i64)
}

/// `float(text)`.
pub fn parse_float(text: &[u8]) -> Option<f64> {
    let text = std::str::from_utf8(text).ok()?.trim_matches(py_space);
    let lower = text.to_ascii_lowercase();
    let unsigned = lower.trim_start_matches(['+', '-']);
    if lower.len() - unsigned.len() <= 1 && matches!(unsigned, "inf" | "infinity" | "nan") {
        return lower.parse().ok();
    }
    if !text.chars().all(|c| c.is_ascii_digit() || "+-._eE".contains(c)) {
        return None;
    }
    // Underscores are legal only between digits.
    let bytes = text.as_bytes();
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'_' {
            let before = i.checked_sub(1).map(|j| bytes[j].is_ascii_digit()).unwrap_or(false);
            let after = bytes.get(i + 1).map(|c| c.is_ascii_digit()).unwrap_or(false);
            if !before || !after {
                return None;
            }
        }
    }
    text.replace('_', "").parse().ok()
}

/// Characters for which Python's `str.isspace()` is true.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}
