//! Narrow read capability launcher: fixed read operations, never arbitrary commands or options.
//! Trusted read launcher (see `tools/robot-exec/test_local_read.py` for the contract).
//!
//! Approved roots can be overridden at build time with LOCAL_READ_PROJECT and LOCAL_READ_TOOLS.

use std::ffi::{CStr, CString};
use std::io::{BufRead, BufReader, Write};
use std::os::fd::FromRawFd;
use std::os::unix::ffi::OsStringExt;
use std::process::exit;

#[path = "../state/ledger.rs"]
mod ledger;

const PROJECT: &str = match option_env!("LOCAL_READ_PROJECT") {
    Some(root) => root,
    None => "/home/mcaniot/Documents/project",
};
const TOOLS: &str = match option_env!("LOCAL_READ_TOOLS") {
    Some(root) => root,
    None => "/home/mcaniot/.agents/tools",
};
const ROOTS: [&str; 2] = [PROJECT, TOOLS];

const PATH_MAX: usize = libc::PATH_MAX as usize;
const INT_MAX: i64 = i32::MAX as i64;
const MAX_FILE_BYTES: i64 = 16 * 1024 * 1024;
const MAX_OUTPUT_BYTES: usize = 65536;
const MAX_LINES: i64 = 40;
const SYS_OPENAT2: libc::c_long = 437;
const RESOLVE_NO_SYMLINKS: u64 = 0x04;
const RESOLVE_BENEATH: u64 = 0x08;

#[repr(C)]
struct OpenHow {
    flags: u64,
    mode: u64,
    resolve: u64,
}

fn reject(reason: &str) -> ! {
    let _ = writeln!(
        std::io::stderr(),
        "local-read: {reason}; use the general executor with normal approval."
    );
    exit(64)
}

fn cstring(bytes: &[u8]) -> CString {
    CString::new(bytes).expect("arguments and paths never contain NUL")
}

/// strtol(base 10) acceptance: C-isspace prefix, optional sign, digits, nothing after.
fn number(text: &[u8], maximum: i64) -> i64 {
    let mut rest = text;
    while let [b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r', tail @ ..] = rest {
        rest = tail;
    }
    let negative = matches!(rest.first(), Some(b'-'));
    if matches!(rest.first(), Some(b'-' | b'+')) {
        rest = &rest[1..];
    }
    let valid = !text.is_empty() && !rest.is_empty() && rest.iter().all(u8::is_ascii_digit);
    let value = rest
        .iter()
        .try_fold(0i64, |acc, digit| acc.checked_mul(10)?.checked_add(i64::from(digit - b'0')));
    match value {
        Some(value) if valid && !negative && (1..=maximum).contains(&value) => value,
        _ => reject("invalid line range"),
    }
}

fn contains_ignore_case(part: &[u8], needle: &[u8]) -> bool {
    part.windows(needle.len()).any(|window| window.eq_ignore_ascii_case(needle))
}

fn contains(part: &[u8], needle: &[u8]) -> bool {
    part.windows(needle.len()).any(|window| window == needle)
}

fn sensitive(path: &[u8]) -> bool {
    if path.len() >= PATH_MAX {
        return true;
    }
    path.split(|&byte| byte == b'/').filter(|part| !part.is_empty()).any(|part| {
        part[0] == b'.'
            || [&b"credential"[..], b"secret", b"password", b"token", b"private"]
                .iter()
                .any(|word| contains_ignore_case(part, word))
            || [&b".pem"[..], b".key", b".p12", b".bak"].iter().any(|word| contains(part, word))
    })
}

fn under_root<'a>(path: &'a [u8], root: &str) -> Option<&'a [u8]> {
    let root = root.as_bytes();
    (path.starts_with(root) && path.get(root.len()) == Some(&b'/')).then(|| &path[root.len() + 1..])
}

/// Validate lexical and resolved names; open beneath an anchored root atomically.
fn source(path: &[u8]) -> i32 {
    let absolute: Vec<u8> = if path.first() == Some(&b'/') {
        if path.len() >= PATH_MAX {
            reject("path too long");
        }
        path.to_vec()
    } else {
        let joined = [PROJECT.as_bytes(), b"/", path].concat();
        if joined.len() >= PATH_MAX {
            reject("path too long");
        }
        joined
    };
    let mut buffer = vec![0u8; PATH_MAX];
    let absolute_c = cstring(&absolute);
    // SAFETY: both pointers are valid and the output buffer holds PATH_MAX bytes as realpath requires.
    let resolved_ptr = unsafe { libc::realpath(absolute_c.as_ptr(), buffer.as_mut_ptr().cast()) };
    if resolved_ptr.is_null() {
        reject("file does not exist");
    }
    // SAFETY: realpath wrote a NUL-terminated string into buffer.
    let resolved = unsafe { CStr::from_ptr(buffer.as_ptr().cast()) }.to_bytes();
    let located = ROOTS.iter().find_map(|root| under_root(resolved, root).map(|relative| (*root, relative)));
    let (root, relative) = match located {
        Some((root, relative)) if !sensitive(relative) => (root, relative),
        _ => reject("path is outside approved source files or is sensitive"),
    };
    // Also reject aliases such as .env -> ordinary-name.py.
    if ROOTS
        .iter()
        .any(|root| under_root(&absolute, root).is_some_and(sensitive))
    {
        reject("sensitive or hidden source path");
    }
    let root_c = cstring(root.as_bytes());
    // SAFETY: root_c is a valid NUL-terminated path.
    let directory = unsafe { libc::open(root_c.as_ptr(), libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC) };
    if directory < 0 {
        reject("cannot open approved root");
    }
    let how = OpenHow {
        flags: (libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NONBLOCK) as u64,
        mode: 0,
        resolve: RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS,
    };
    let relative_c = cstring(relative);
    // SAFETY: arguments match the openat2(2) contract; how outlives the call.
    let fd = unsafe {
        libc::syscall(SYS_OPENAT2, directory, relative_c.as_ptr(), &how as *const OpenHow, size_of::<OpenHow>())
    } as i32;
    // SAFETY: directory is an fd opened above.
    unsafe { libc::close(directory) };
    // SAFETY: zeroed stat is a valid out-parameter for fstat.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: st is a valid out-parameter; fstat tolerates invalid fds by failing.
    let acceptable = fd >= 0
        && unsafe { libc::fstat(fd, &mut st) } == 0
        && st.st_mode & libc::S_IFMT == libc::S_IFREG
        && st.st_nlink == 1
        && st.st_uid == unsafe { libc::getuid() }
        && st.st_size <= MAX_FILE_BYTES;
    if !acceptable {
        reject("source must be a regular file up to 16 MiB (openat2 required)");
    }
    fd
}

fn literal_regex(pattern: &[u8]) -> regex::bytes::Regex {
    let escaped: String = pattern.iter().map(|byte| format!("\\x{byte:02x}")).collect();
    regex::bytes::Regex::new(&format!("(?-u){escaped}")).expect("hex-escaped literal is a valid pattern")
}

fn read_source(args: &[Vec<u8>], find: bool) -> i32 {
    let argc = args.len();
    if (!find && !(3..=5).contains(&argc)) || (find && argc != 4) {
        reject("invalid read arguments");
    }
    let path = if find { &args[3] } else { &args[2] };
    let pattern = find.then(|| &args[2]);
    // strlen stops at NUL; arguments never contain one.
    if pattern.is_some_and(|pattern| pattern.is_empty() || pattern.len() > 512) {
        reject("literal must contain 1 to 512 bytes");
    }
    let start = if !find && argc >= 4 { number(&args[3], INT_MAX) } else { 1 };
    let count = if !find && argc >= 5 { number(&args[4], MAX_LINES) } else { MAX_LINES };
    let matcher = pattern.map(|pattern| literal_regex(pattern));
    // SAFETY: source returns a freshly opened fd that this File now owns.
    let file = unsafe { std::fs::File::from_raw_fd(source(path)) };
    let meta = file.metadata().ok();
    let mut stream = BufReader::new(file);
    let mut line = Vec::new();
    let mut output = Vec::new();
    let (mut bytes, mut index, mut shown) = (0usize, 0i64, 0i64);
    let mut failed = false;
    loop {
        line.clear();
        match stream.read_until(b'\n', &mut line) {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => {
                failed = true;
                break;
            }
        }
        index += 1;
        if index < start {
            continue;
        }
        if let Some(matcher) = &matcher {
            let text = line.split(|&byte| byte == 0).next().unwrap_or_default();
            if !matcher.is_match(text) {
                continue;
            }
        }
        let length = line.len().min(MAX_OUTPUT_BYTES - bytes);
        if find {
            output.extend_from_slice(format!("{index}:").as_bytes());
        }
        output.extend_from_slice(&line[..length]);
        bytes += length;
        shown += 1;
        if shown >= count || bytes >= MAX_OUTPUT_BYTES {
            break;
        }
    }
    let mut stdout = std::io::stdout().lock();
    let _ = stdout.write_all(&output);
    let _ = stdout.flush();
    if let (false, false, Some(meta), Some(session)) = (find, failed, meta, SESSION.get()) {
        record_read(session, path, &meta, start, shown);
    }
    i32::from(failed)
}

/// Claude session id, captured before the environment is cleared.
static SESSION: std::sync::OnceLock<String> = std::sync::OnceLock::new();

/// Best effort: note the lines shown in the shared read ledger; errors are ignored.
fn record_read(session: &str, path: &[u8], meta: &std::fs::Metadata, start: i64, shown: i64) {
    if shown < 1 || start < 1 {
        return;
    }
    let Some(state) = ledger::state_file(std::path::Path::new("/home/mcaniot"), "read-ledger", session) else { return };
    let path = std::path::PathBuf::from(std::ffi::OsString::from_vec(path.to_vec()));
    let (first, last) = (start as u64, (start + shown - 1) as u64);
    let mut book = ledger::Ledger::load(&state);
    book.record(&ledger::key(&path), ledger::stamp(meta), first, last);
    book.save(&state);
}

fn trusted_environment() {
    // SAFETY: single-threaded process; strings are valid NUL-terminated literals.
    let established = unsafe {
        libc::clearenv() == 0
            && libc::setenv(c"HOME".as_ptr(), c"/home/mcaniot".as_ptr(), 1) == 0
            && libc::setenv(c"PATH".as_ptr(), c"/usr/bin:/bin".as_ptr(), 1) == 0
    };
    if !established {
        reject("cannot establish trusted helper environment");
    }
}

fn main() {
    // Rust ignores SIGPIPE by default; the C launcher relies on the default disposition.
    // SAFETY: restoring the default handler is always valid.
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };
    if let Ok(session) = std::env::var("CLAUDE_CODE_SESSION_ID") {
        let _ = SESSION.set(session);
    }
    trusted_environment();
    let args: Vec<Vec<u8>> = std::env::args_os().map(OsStringExt::into_vec).collect();
    let argc = args.len();
    if argc >= 2 && args[1] == b"--internal-text" {
        exit(read_source(&args, false));
    }
    if argc >= 2 && args[1] == b"--internal-find" {
        exit(read_source(&args, true));
    }
    if argc < 2 {
        reject("expected status, files, text or find");
    }
    let mut buffer = vec![0u8; PATH_MAX];
    // SAFETY: the buffer holds PATH_MAX bytes and at most PATH_MAX - 1 are requested.
    let size = unsafe { libc::readlink(c"/proc/self/exe".as_ptr(), buffer.as_mut_ptr().cast(), PATH_MAX - 1) };
    if size < 0 {
        reject("cannot locate launcher");
    }
    let me = &buffer[..size as usize];
    let Some(slash) = me.iter().rposition(|&byte| byte == b'/') else {
        reject("cannot locate launcher directory")
    };
    let runner = [&me[..slash], b"/native/run"].concat();
    if runner.len() >= PATH_MAX {
        reject("runner path too long");
    }
    let mut command: Vec<Vec<u8>> = ["run", "--native-only", "--cwd", PROJECT, "--timeout", "30", "--max-log-bytes", "1048576", "--"]
        .iter()
        .map(|part| part.as_bytes().to_vec())
        .collect();
    command.insert(0, runner.clone());
    let verb = args[1].as_slice();
    if verb == b"status" && argc == 2 {
        for part in [
            "/usr/bin/git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c",
            "core.hooksPath=/dev/null", "-C", PROJECT, "status", "--short", "--branch", "--ignore-submodules=all",
        ] {
            command.push(part.as_bytes().to_vec());
        }
    } else if verb == b"files" && argc == 2 {
        for part in ["/usr/bin/rg", "--files", "--", PROJECT] {
            command.push(part.as_bytes().to_vec());
        }
    } else if verb == b"text" && (3..=5).contains(&argc) {
        // SAFETY: source returned an fd owned here.
        unsafe { libc::close(source(&args[2])) };
        if argc >= 4 {
            number(&args[3], INT_MAX);
        }
        if argc >= 5 {
            number(&args[4], MAX_LINES);
        }
        command.push(me.to_vec());
        command.push(b"--internal-text".to_vec());
        command.extend(args[2..].iter().cloned());
    } else if verb == b"find" && argc == 4 {
        // SAFETY: source returned an fd owned here.
        unsafe { libc::close(source(&args[3])) };
        if args[2].is_empty() || args[2].len() > 512 {
            reject("literal must contain 1 to 512 bytes");
        }
        command.push(me.to_vec());
        command.push(b"--internal-find".to_vec());
        command.push(args[2].clone());
        command.push(args[3].clone());
    } else {
        reject("unsupported operation or arguments");
    }
    let runner_c = cstring(&runner);
    let owned: Vec<CString> = command.iter().map(|part| cstring(part)).collect();
    let mut pointers: Vec<*const libc::c_char> = owned.iter().map(|part| part.as_ptr()).collect();
    pointers.push(std::ptr::null());
    // SAFETY: runner_c and every pointer in the NULL-terminated vector outlive the call.
    unsafe { libc::execv(runner_c.as_ptr(), pointers.as_ptr()) };
    let error = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
    // SAFETY: strerror returns a valid NUL-terminated string.
    let text = unsafe { CStr::from_ptr(libc::strerror(error)) }.to_string_lossy().into_owned();
    let _ = writeln!(std::io::stderr(), "local-read: exec native runner: {text}");
    exit(127);
}
