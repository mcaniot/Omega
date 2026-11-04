//! Single-threaded POSIX evidence executor. It grants no execution permissions.
//! Every evidence operation is anchored to nofollow descriptors.

use crate::argparse::Arg;
use crate::diag::{self, Summary};
use crate::fail;
use crate::py::{self, Style};
use std::ffi::{CStr, CString};
use std::io::Write;
use std::os::unix::ffi::OsStrExt;

const PATH_MAX: usize = 4096;
const BUDGET: usize = 4000;

pub struct Config {
    pub command: Vec<Arg>,
    pub cwd: Arg,
    pub home: Arg,
    /// Seconds; 0 means no timeout.
    pub timeout: f64,
    pub grace: f64,
    pub inline_bytes: i64,
    pub max_log_bytes: i64,
    /// Reached through the argparse path: Python-style messages and results.
    pub python: bool,
}

unsafe extern "C" {
    fn sigabbrev_np(sig: libc::c_int) -> *const libc::c_char;
}

fn join(parent: &[u8], leaf: &[u8]) -> Vec<u8> {
    let mut p = parent.to_vec();
    p.push(b'/');
    p.extend_from_slice(leaf);
    if p.len() >= PATH_MAX {
        fail("Path too long");
    }
    p
}

fn now() -> f64 {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t) };
    t.tv_sec as f64 + t.tv_nsec as f64 / 1e9
}

fn gmtime(sec: libc::time_t) -> libc::tm {
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::gmtime_r(&sec, &mut tm) };
    tm
}

fn strftime(format: &CStr, tm: &libc::tm) -> String {
    let mut buf = [0u8; 64];
    let n = unsafe { libc::strftime(buf.as_mut_ptr().cast(), buf.len(), format.as_ptr(), tm) };
    String::from_utf8_lossy(&buf[..n]).into_owned()
}

fn timestamp() -> String {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_REALTIME, &mut t) };
    let date = strftime(c"%Y-%m-%dT%H:%M:%S", &gmtime(t.tv_sec));
    format!("{}.{:06}+00:00", date, t.tv_nsec / 1000)
}

fn format_float(format: &CStr, x: f64) -> String {
    let mut buf = [0u8; 64];
    let n = unsafe { libc::snprintf(buf.as_mut_ptr().cast(), buf.len(), format.as_ptr(), x) };
    String::from_utf8_lossy(&buf[..n as usize]).into_owned()
}

fn size_of(fd: i32) -> i64 {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } != 0 {
        fail("Cannot stat evidence");
    }
    st.st_size
}

fn signal_name(sig: i32) -> String {
    let abbr = unsafe { sigabbrev_np(sig) };
    let name = if abbr.is_null() {
        "UNKNOWN".into()
    } else {
        unsafe { CStr::from_ptr(abbr) }.to_string_lossy().into_owned()
    };
    format!("SIG{}", name)
}

fn alive(group: libc::pid_t) -> bool {
    unsafe { libc::kill(-group, 0) == 0 || py::errno() == libc::EPERM }
}

fn send_group(group: libc::pid_t, sig: i32) {
    unsafe { libc::kill(-group, sig) };
}

fn nap(seconds: f64) {
    let mut t = libc::timespec { tv_sec: seconds as libc::time_t, tv_nsec: ((seconds - seconds.trunc()) * 1e9) as _ };
    while unsafe { libc::nanosleep(&t, &mut t) } != 0 && py::errno() == libc::EINTR {}
}

fn read_byte(fd: i32) -> Option<u8> {
    let mut c = 0u8;
    loop {
        let n = unsafe { libc::read(fd, (&mut c as *mut u8).cast(), 1) };
        if n < 0 && py::errno() == libc::EINTR {
            continue;
        }
        return (n == 1).then_some(c);
    }
}

fn write_all(fd: i32, data: &[u8]) -> bool {
    let mut at = 0;
    while at < data.len() {
        let n = unsafe { libc::write(fd, data[at..].as_ptr().cast(), data.len() - at) };
        if n < 0 && py::errno() == libc::EINTR {
            continue;
        }
        if n <= 0 {
            return false;
        }
        at += n as usize;
    }
    true
}

fn private_create(dir: i32, name: &CStr, flags: i32) -> i32 {
    unsafe {
        libc::openat(
            dir,
            name.as_ptr(),
            flags | libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            0o600,
        )
    }
}

/// Exception class Python would raise for an `errno`.
fn error_class(errno: i32) -> &'static str {
    match errno {
        libc::ENOENT => "FileNotFoundError",
        libc::EACCES | libc::EPERM => "PermissionError",
        libc::ENOTDIR => "NotADirectoryError",
        _ => "OSError",
    }
}

struct Run<'a> {
    cfg: &'a Config,
    command: Vec<CString>,
    cwd: Vec<u8>,
    directory: Vec<u8>,
    outpath: Vec<u8>,
    errpath: Vec<u8>,
    started: String,
    start: f64,
    evidence_fd: i32,
    metadata_fd: i32,
    child: libc::pid_t,
}

impl Run<'_> {
    fn metadata(&mut self, status: &str, code: i32, returncode: i32, received: i32, crash_signal: i32, cleanup: bool, sizes: (i64, i64)) {
        let mut b: Vec<u8> = Vec::new();
        let quote = |b: &mut Vec<u8>, s: &[u8]| py::json_str(b, s, Style::C);
        b.extend_from_slice(b"{\"schema\":1,\"status\":");
        quote(&mut b, status.as_bytes());
        for (key, value) in [
            ("cwd", self.cwd.as_slice()),
            ("started_at", self.started.as_bytes()),
            ("stdout", &self.outpath),
            ("stderr", &self.errpath),
        ] {
            b.extend_from_slice(format!(",\"{}\":", key).as_bytes());
            quote(&mut b, value);
        }
        b.extend_from_slice(format!(",\"wrapper_pid\":{},\"timeout_seconds\":", std::process::id()).as_bytes());
        if self.cfg.timeout != 0.0 {
            b.extend_from_slice(format_float(c"%.9g", self.cfg.timeout).as_bytes());
        } else {
            b.extend_from_slice(b"null");
        }
        b.extend_from_slice(b",\"command\":[");
        for (i, arg) in self.command.iter().enumerate() {
            if i > 0 {
                b.push(b',');
            }
            quote(&mut b, arg.to_bytes());
        }
        b.push(b']');
        if self.child != 0 {
            b.extend_from_slice(format!(",\"pid\":{0},\"process_group\":{0}", self.child).as_bytes());
        }
        let finished = status != "RUNNING";
        if finished {
            b.extend_from_slice(b",\"finished_at\":");
            quote(&mut b, timestamp().as_bytes());
            b.extend_from_slice(
                format!(
                    ",\"exit_code\":{},\"child_returncode\":{},\"duration_seconds\":{},\"interrupted_signal\":",
                    code,
                    returncode,
                    format_float(c"%.4f", now() - self.start)
                )
                .as_bytes(),
            );
            if received != 0 {
                b.extend_from_slice(received.to_string().as_bytes());
            } else {
                b.extend_from_slice(b"null");
            }
            b.extend_from_slice(b",\"signal\":");
            if crash_signal != 0 {
                quote(&mut b, signal_name(crash_signal).as_bytes());
            } else {
                b.extend_from_slice(b"null");
            }
            b.extend_from_slice(
                format!(
                    ",\"background_children_terminated\":{},\"bytes\":{{\"stdout\":{},\"stderr\":{}}}",
                    cleanup, sizes.0, sizes.1
                )
                .as_bytes(),
            );
        }
        b.extend_from_slice(b"}\n");
        let fd = private_create(self.evidence_fd, c"result.tmp", 0);
        if fd < 0 || !write_all(fd, &b) {
            fail("Cannot write metadata");
        }
        if finished {
            self.metadata_fd = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) };
            if self.metadata_fd < 0 {
                fail("Cannot retain completed metadata descriptor");
            }
        }
        if unsafe { libc::close(fd) } != 0
            || unsafe { libc::renameat(self.evidence_fd, c"result.tmp".as_ptr(), self.evidence_fd, c"result.json".as_ptr()) } != 0
        {
            fail("Cannot finalize metadata");
        }
    }

    /// Stops the command group if the wrapper dies without finalizing.
    fn sentinel(&self, out: i32, err: i32, oldmask: &libc::sigset_t) -> (libc::pid_t, i32) {
        let mut pipes = [0i32; 2];
        if unsafe { libc::pipe2(pipes.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
            fail("Cannot create sentinel pipe");
        }
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            fail("Cannot fork sentinel");
        }
        if pid == 0 {
            unsafe {
                libc::close(pipes[1]);
                libc::close(out);
                libc::close(err);
                libc::setsid();
                libc::sigprocmask(libc::SIG_SETMASK, oldmask, std::ptr::null_mut());
                let null = libc::open(c"/dev/null".as_ptr(), libc::O_RDWR);
                if null >= 0 {
                    libc::dup2(null, 0);
                    libc::dup2(null, 1);
                    libc::dup2(null, 2);
                    if null > 2 {
                        libc::close(null);
                    }
                }
            }
            if read_byte(pipes[0]) != Some(b'd') {
                send_group(self.child, libc::SIGTERM);
                nap(self.cfg.grace);
                send_group(self.child, libc::SIGKILL);
            }
            unsafe { libc::_exit(0) };
        }
        unsafe { libc::close(pipes[0]) };
        (pid, pipes[1])
    }

    /// Runs in the forked command process; never returns.
    fn exec_child(&self, gate: [i32; 2], oldmask: &libc::sigset_t, out: i32, err: i32) -> ! {
        unsafe {
            libc::close(gate[1]);
            let ready = read_byte(gate[0]);
            libc::close(gate[0]);
            if ready != Some(b'g') || libc::setsid() < 0 {
                libc::_exit(126);
            }
            libc::sigprocmask(libc::SIG_SETMASK, oldmask, std::ptr::null_mut());
            let null = libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY);
            if null >= 0 {
                libc::dup2(null, 0);
                if null > 2 {
                    libc::close(null);
                }
            }
            libc::dup2(out, 1);
            libc::dup2(err, 2);
            libc::close(out);
            libc::close(err);
        }
        let python = self.cfg.python;
        let cwd = py::cstring(&self.cwd);
        if unsafe { libc::chdir(cwd.as_ptr()) } != 0 {
            let e = py::errno();
            let text = if python {
                format!("{}: {}\n", error_class(e), py::oserror(e, &self.cwd))
            } else {
                format!("OSError: {}\n", py::strerror(e))
            };
            write_all(2, text.as_bytes());
            unsafe { libc::_exit(if python && e == libc::ENOENT { 127 } else { 126 }) };
        }
        let mut argv: Vec<*const libc::c_char> = self.command.iter().map(|a| a.as_ptr()).collect();
        argv.push(std::ptr::null());
        unsafe { libc::execvp(argv[0], argv.as_ptr()) };
        let e = py::errno();
        let name = self.command[0].to_bytes();
        let text = if python {
            format!("{}: {}\n", error_class(e), py::oserror(e, name))
        } else {
            let class = if e == libc::ENOENT { "FileNotFoundError" } else { "OSError" };
            format!("{}: {}: {}\n", class, String::from_utf8_lossy(name), py::strerror(e))
        };
        write_all(2, text.as_bytes());
        unsafe { libc::_exit(if e == libc::ENOENT { 127 } else { 126 }) };
    }

    fn pending_signals(&self, mask: &libc::sigset_t, received: &mut i32) {
        let immediate = libc::timespec { tv_sec: 0, tv_nsec: 0 };
        loop {
            let sig = unsafe { libc::sigtimedwait(mask, std::ptr::null_mut(), &immediate) };
            if sig < 0 {
                return;
            }
            if matches!(sig, libc::SIGINT | libc::SIGTERM | libc::SIGHUP) {
                if *received == 0 {
                    *received = sig;
                }
                send_group(self.child, sig);
            }
        }
    }
}

fn open_dir(parent: Option<i32>, path: &CStr) -> i32 {
    let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    unsafe {
        match parent {
            Some(fd) => libc::openat(fd, path.as_ptr(), flags),
            None => libc::open(path.as_ptr(), flags),
        }
    }
}

fn emit(bytes: &[u8]) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(bytes);
    let _ = out.flush();
}

/// Native fast-path summary; falls back to the complete form for anything it
/// cannot represent exactly.
fn summary(cfg: &Config, code: i32, crash_signal: i32, sizes: (i64, i64), status: &str, elapsed: f64, directory: &[u8], fds: (i32, i32)) {
    let duration = format_float(c"%.4f", elapsed);
    let mut s = Summary {
        status: status.into(),
        exit_code: code,
        duration,
        bytes: sizes,
        signal: (crash_signal != 0).then(|| signal_name(crash_signal)),
        evidence: directory.to_vec(),
        stdout: None,
        stderr: None,
        diagnostics: Vec::new(),
    };
    let total = sizes.0 + sizes.1;
    let mut simple = !cfg.python;
    if total <= cfg.inline_bytes && total <= BUDGET as i64 {
        for (value, size, fd) in [(&mut s.stdout, sizes.0, fds.0), (&mut s.stderr, sizes.1, fds.1)] {
            let data = diag::read_fd(fd);
            if data.iter().any(|&b| b >= 128 || b == 0 || b == 27) {
                simple = false;
            }
            if size > 0 {
                *value = Some(String::from_utf8_lossy(&data).into_owned());
            }
        }
    } else if code != 0 {
        simple = false;
    }
    if simple {
        let mut line = s.encode(Style::C);
        line.push(b'\n');
        if line.len() <= BUDGET + 1 {
            emit(&line);
            return;
        }
    }
    s.duration = py::pyfloat_repr(s.duration.parse().unwrap_or(elapsed));
    let s = diag::finalize(s, cfg.inline_bytes, fds.0, fds.1);
    let mut line = s.encode(Style::Py);
    line.push(b'\n');
    emit(&line);
}

pub fn execute(cfg: Config) -> i32 {
    let command: Vec<CString> = cfg.command.iter().map(|a| py::cstring(a)).collect();
    let tasks = join(&cfg.home, b".agents/tasks");
    if cfg.python {
        let _ = std::fs::create_dir_all(std::ffi::OsStr::from_bytes(&tasks));
    }
    let tasks_fd = open_dir(None, &py::cstring(&tasks));
    if tasks_fd < 0 {
        fail("Cannot open trusted tasks directory");
    }
    if unsafe { libc::mkdirat(tasks_fd, c"evidence".as_ptr(), 0o700) } != 0 && py::errno() != libc::EEXIST {
        fail("Cannot create evidence root");
    }
    let root_fd = open_dir(Some(tasks_fd), c"evidence");
    unsafe { libc::close(tasks_fd) };
    if root_fd < 0 {
        fail("Cannot open evidence root without following symlinks");
    }
    if unsafe { libc::fchmod(root_fd, 0o700) } != 0 {
        fail("Cannot protect evidence root");
    }
    let root = join(&tasks, b"evidence");
    let prefix = strftime(c"%Y%m%dT%H%M%S-native-XXXXXX", &gmtime(unsafe { libc::time(std::ptr::null_mut()) }));
    let anchored = join(format!("/proc/self/fd/{}", root_fd).as_bytes(), prefix.as_bytes());
    let template = py::cstring(&anchored).into_raw();
    if unsafe { libc::mkdtemp(template) }.is_null() {
        fail("Cannot create evidence directory");
    }
    let anchored = unsafe { CString::from_raw(template) };
    let anchored = anchored.to_bytes();
    let leaf = &anchored[anchored.iter().rposition(|&b| b == b'/').unwrap() + 1..];
    let evidence_fd = open_dir(Some(root_fd), &py::cstring(leaf));
    unsafe { libc::close(root_fd) };
    if evidence_fd < 0 {
        fail("Cannot open private evidence directory");
    }
    let directory = join(&root, leaf);
    let out = private_create(evidence_fd, c"stdout.log", 0);
    let err = private_create(evidence_fd, c"stderr.log", 0);
    if out < 0 || err < 0 {
        fail("Cannot create captured logs");
    }
    let mut run = Run {
        outpath: join(&directory, b"stdout.log"),
        errpath: join(&directory, b"stderr.log"),
        cwd: cfg.cwd.clone(),
        directory,
        started: timestamp(),
        start: now(),
        evidence_fd,
        metadata_fd: -1,
        child: 0,
        command,
        cfg: &cfg,
    };
    run.metadata("RUNNING", 0, 0, 0, 0, false, (0, 0));
    let mut mask: libc::sigset_t = unsafe { std::mem::zeroed() };
    let mut oldmask: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut mask);
        for sig in [libc::SIGCHLD, libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            libc::sigaddset(&mut mask, sig);
        }
        if libc::sigprocmask(libc::SIG_BLOCK, &mask, &mut oldmask) != 0 {
            fail("Cannot block monitoring signals");
        }
    }
    let mut gate = [0i32; 2];
    if unsafe { libc::pipe2(gate.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        fail("Cannot create startup pipe");
    }
    run.child = unsafe { libc::fork() };
    if run.child < 0 {
        fail("Cannot fork command");
    }
    if run.child == 0 {
        run.exec_child(gate, &oldmask, out, err);
    }
    unsafe { libc::close(gate[0]) };
    let (guard, writer) = run.sentinel(out, err, &oldmask);
    if !write_all(gate[1], b"g") {
        fail("Cannot release command");
    }
    unsafe { libc::close(gate[1]) };
    run.metadata("RUNNING", 0, 0, 0, 0, false, (0, 0));
    let child = run.child;
    let (mut ws, mut received) = (0i32, 0i32);
    let (mut timed_out, mut limited, mut cleanup) = (false, false, false);
    let mut stop = 0.0;
    loop {
        let done = unsafe { libc::waitpid(child, &mut ws, libc::WNOHANG) };
        if done == child {
            break;
        }
        if done < 0 && py::errno() != libc::EINTR {
            fail("Cannot wait for command");
        }
        let elapsed = now() - run.start;
        if received == 0 && cfg.timeout != 0.0 && elapsed >= cfg.timeout && !timed_out {
            timed_out = true;
            send_group(child, libc::SIGTERM);
        }
        if received == 0 && !timed_out && !limited && (size_of(out) > cfg.max_log_bytes || size_of(err) > cfg.max_log_bytes) {
            limited = true;
            send_group(child, libc::SIGTERM);
        }
        if (received != 0 || timed_out || limited) && stop == 0.0 {
            stop = now();
        }
        if stop != 0.0 && now() - stop >= cfg.grace {
            send_group(child, libc::SIGKILL);
        }
        let tick = libc::timespec { tv_sec: 0, tv_nsec: 25_000_000 };
        let sig = unsafe { libc::sigtimedwait(&mask, std::ptr::null_mut(), &tick) };
        if matches!(sig, libc::SIGINT | libc::SIGTERM | libc::SIGHUP) {
            if received == 0 {
                received = sig;
            }
            send_group(child, sig);
        }
    }
    if alive(child) {
        cleanup = true;
        send_group(child, libc::SIGTERM);
        let deadline = now() + cfg.grace;
        while alive(child) && now() < deadline {
            run.pending_signals(&mask, &mut received);
            nap(0.025);
        }
        send_group(child, libc::SIGKILL);
    }
    if !write_all(writer, b"d") {
        fail("Cannot finalize sentinel");
    }
    unsafe { libc::close(writer) };
    while unsafe { libc::waitpid(guard, std::ptr::null_mut(), 0) } < 0 && py::errno() == libc::EINTR {}
    // Control signals stay blocked through finalization so a late one cannot
    // terminate the wrapper before its evidence is complete.
    run.pending_signals(&mask, &mut received);
    let returncode = if libc::WIFSIGNALED(ws) { -libc::WTERMSIG(ws) } else { libc::WEXITSTATUS(ws) };
    let crash_signal = if returncode < 0 && received == 0 && !timed_out && !limited { -returncode } else { 0 };
    let code = if timed_out {
        124
    } else if limited {
        125
    } else if received != 0 {
        128 + received
    } else if returncode < 0 {
        128 - returncode
    } else {
        returncode
    };
    let status = if timed_out {
        "TIMEOUT"
    } else if limited {
        "LOG_LIMIT"
    } else if received != 0 {
        "INTERRUPTED"
    } else if crash_signal != 0 {
        "CRASHED"
    } else if code != 0 {
        "FAIL"
    } else {
        "PASS"
    };
    let sizes = (size_of(out), size_of(err));
    run.metadata(status, code, returncode, received, crash_signal, cleanup, sizes);
    summary(&cfg, code, crash_signal, sizes, status, now() - run.start, &run.directory, (out, err));
    unsafe {
        libc::close(out);
        libc::close(err);
        libc::close(run.metadata_fd);
        libc::close(evidence_fd);
    }
    code
}
