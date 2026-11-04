//! Native evidence executor: `run`, `inspect` and `prune` in one binary.
//!
//! `run` first applies the strict native option grammar (exact options with a
//! separate value). Anything outside it is parsed with the argparse-compatible
//! parser instead, so abbreviations, `--opt=value` and usage errors behave as
//! the original Python CLI did. `--native-only` rejects that second path.

mod argparse;
mod diag;
mod exec;
mod inspect;
mod prune;
mod py;

use argparse::Arg;
use exec::Config;
use py::Style;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::sync::atomic::{AtomicBool, Ordering};

static PYTHON_ERRORS: AtomicBool = AtomicBool::new(false);

fn write_stdout(bytes: &[u8]) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(bytes);
    let _ = out.flush();
}

fn failure_line(message: &str, style: Style) -> Vec<u8> {
    let mut b = Vec::new();
    if style == Style::C {
        b.extend_from_slice(b"{\"status\":\"FAIL\",\"error\":");
    } else {
        b.extend_from_slice(b"{\"status\": \"FAIL\", \"error\": ");
    }
    py::json_str(&mut b, message.as_bytes(), style);
    b.extend_from_slice(b"}\n");
    b
}

/// Reports a failed operation as JSON and exits with status 2.
pub fn fail(message: &str) -> ! {
    let style = if PYTHON_ERRORS.load(Ordering::Relaxed) { Style::PyAscii } else { Style::C };
    write_stdout(&failure_line(message, style));
    std::process::exit(2);
}

fn number(text: &[u8]) -> f64 {
    let c = py::cstring(text);
    let mut end: *mut libc::c_char = std::ptr::null_mut();
    unsafe { *libc::__errno_location() = 0 };
    let x = unsafe { libc::strtod(c.as_ptr(), &mut end) };
    if py::errno() != 0 || end.cast_const() == c.as_ptr() || unsafe { *end } != 0 || !x.is_finite() {
        fail("Invalid numeric option");
    }
    x
}

fn integer(text: &[u8]) -> i64 {
    let c = py::cstring(text);
    let mut end: *mut libc::c_char = std::ptr::null_mut();
    unsafe { *libc::__errno_location() = 0 };
    let x = unsafe { libc::strtoll(c.as_ptr(), &mut end, 10) };
    if py::errno() != 0 || end.cast_const() == c.as_ptr() || unsafe { *end } != 0 {
        fail("Invalid integer option");
    }
    x
}

fn home() -> Option<Arg> {
    if let Some(h) = std::env::var_os("HOME") {
        return Some(h.as_bytes().to_vec());
    }
    let pw = unsafe { libc::getpwuid(libc::getuid()) };
    if pw.is_null() {
        return None;
    }
    Some(unsafe { std::ffi::CStr::from_ptr((*pw).pw_dir) }.to_bytes().to_vec())
}

/// Strict native grammar. `None` means the input needs the argparse path.
fn native_config(args: &[Arg]) -> Option<Config> {
    if args.iter().flatten().any(|&b| b >= 128) {
        return None;
    }
    let (mut cwd_arg, mut shell): (Option<&Arg>, Option<&Arg>) = (None, None);
    let mut shell_executable: Arg = b"/bin/bash".to_vec();
    let (mut timeout, mut grace, mut inline_bytes, mut max_log_bytes) = (0.0, 10.0, 8192i64, 2i64 << 30);
    let mut pos = 0;
    while pos < args.len() {
        let arg = args[pos].as_slice();
        if arg == b"--" {
            pos += 1;
            break;
        }
        if arg.first() != Some(&b'-') || arg == b"-" {
            break;
        }
        if pos + 1 >= args.len() {
            return None;
        }
        let value = &args[pos + 1];
        match arg {
            b"--cwd" => cwd_arg = Some(value),
            b"--timeout" => {
                timeout = number(value);
                if timeout <= 0.0 {
                    fail("--timeout must be positive");
                }
            }
            b"--kill-after" => {
                grace = number(value);
                if grace <= 0.0 {
                    fail("--kill-after must be positive");
                }
            }
            b"--inline-bytes" => inline_bytes = integer(value),
            b"--max-log-bytes" => {
                max_log_bytes = integer(value);
                if max_log_bytes <= 0 {
                    fail("--max-log-bytes must be positive");
                }
            }
            b"--shell" => shell = Some(value),
            b"--shell-executable" => shell_executable = value.clone(),
            _ => return None,
        }
        pos += 2;
    }
    let command: Vec<Arg> = match shell {
        Some(script) => {
            if pos < args.len() {
                fail("Use either --shell or argv after --, not both");
            }
            vec![shell_executable, b"-c".to_vec(), script.clone()]
        }
        None => {
            if pos >= args.len() {
                fail("A command is required after --");
            }
            args[pos..].to_vec()
        }
    };
    let cwd = std::fs::canonicalize(std::ffi::OsStr::from_bytes(cwd_arg.map_or(b".".as_slice(), |c| c.as_slice()))).ok()?;
    let cwd = cwd.as_os_str().as_bytes().to_vec();
    let home = home()?;
    if home.iter().chain(&cwd).any(|&b| b >= 128) {
        return None;
    }
    Some(Config { command, cwd, home, timeout, grace, inline_bytes, max_log_bytes, python: false })
}

fn python_run(p: &argparse::Parsed) -> Config {
    let mut command = p.list("command");
    if command.first().map(Vec::as_slice) == Some(b"--") {
        command.remove(0);
    }
    if let Some(script) = p.string("shell") {
        if !command.is_empty() {
            fail("Use either --shell or argv after --, not both");
        }
        command = vec![p.string("shell_executable").unwrap_or_else(|| b"/bin/bash".to_vec()), b"-c".to_vec(), script];
    }
    if command.is_empty() {
        fail("A command is required after --");
    }
    let timeout = p.float("timeout");
    if timeout.is_some_and(|t| t <= 0.0) {
        fail("--timeout must be positive");
    }
    let grace = p.float("kill_after").unwrap_or(10.0);
    let max_log_bytes = p.int("max_log_bytes").unwrap_or(2 << 30);
    if grace <= 0.0 || max_log_bytes <= 0 {
        fail("--kill-after and --max-log-bytes must be positive");
    }
    let Some(home) = home() else { fail("Cannot determine home directory") };
    Config {
        command,
        cwd: py::resolve(&p.string("cwd").unwrap_or_else(|| b".".to_vec())),
        home,
        timeout: timeout.unwrap_or(0.0),
        grace,
        inline_bytes: p.int("inline_bytes").unwrap_or(8192),
        max_log_bytes,
        python: true,
    }
}

fn dispatch(args: &[Arg]) -> i32 {
    if args.first().map(Vec::as_slice) == Some(b"run") {
        let native_only = args.get(1).map(Vec::as_slice) == Some(b"--native-only");
        let rest = &args[if native_only { 2 } else { 1 }..];
        if let Some(cfg) = native_config(rest) {
            return exec::execute(cfg);
        }
        if native_only {
            fail("Unsupported input for native-only execution");
        }
    }
    PYTHON_ERRORS.store(true, Ordering::Relaxed);
    let (label, _, parsed) = match argparse::parse_command(args) {
        Ok(v) => v,
        Err(exit) => {
            write_stdout(&exit.out);
            let _ = std::io::stderr().write_all(&exit.err);
            return exit.code;
        }
    };
    let result = match label {
        "run" => return exec::execute(python_run(&parsed)),
        "inspect" => inspect::inspect(&parsed),
        _ => match home() {
            Some(h) => prune::prune(&parsed, &h),
            None => Err("Cannot determine home directory".into()),
        },
    };
    match result {
        Ok(mut line) => {
            line.push(b'\n');
            write_stdout(&line);
            0
        }
        Err(message) => {
            write_stdout(&failure_line(&message, Style::PyAscii));
            2
        }
    }
}

fn main() {
    // Rust ignores SIGPIPE; commands started by exec must inherit the default.
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };
    let mut argv = std::env::args_os();
    let executable = argv.next().unwrap_or_default();
    let mut args: Vec<Arg> = argv.map(|a| a.as_bytes().to_vec()).collect();
    let command = match std::path::Path::new(&executable).file_name().and_then(|s| s.to_str()) {
        Some("omega-run" | "Ω-run") => Some(b"run".as_slice()),
        Some("omega-inspect" | "Ω-inspect") => Some(b"inspect".as_slice()),
        Some("omega-prune" | "Ω-prune") => Some(b"prune".as_slice()),
        _ => None,
    };
    if let Some(command) = command {
        args.insert(0, command.to_vec());
    }
    std::process::exit(dispatch(&args));
}
