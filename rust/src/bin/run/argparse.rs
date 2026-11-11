//! The subset of Python's argparse needed by the original `run.py` CLI:
//! subcommands, abbreviations, `--opt=value`, REMAINDER and usage errors.

use crate::py;

pub type Arg = Vec<u8>;

pub enum Kind {
    Help,
    Flag,
    Str,
    Int,
    Float,
    Choice(&'static [&'static str]),
}

pub struct Opt {
    pub names: &'static [&'static str],
    pub dest: &'static str,
    pub kind: Kind,
}

pub enum Pos {
    None,
    One(&'static str),
    Remainder(&'static str),
    Command(&'static [&'static str]),
}

pub struct Parser {
    pub prog: &'static str,
    pub usage: &'static str,
    pub help: &'static str,
    pub opts: &'static [Opt],
    pub pos: Pos,
}

#[derive(Clone, Debug)]
pub enum Val {
    Flag,
    Str(Arg),
    Int(i64),
    Float(f64),
    List(Vec<Arg>),
}

#[derive(Default)]
pub struct Parsed {
    vals: Vec<(&'static str, Val)>,
    pub extras: Vec<Arg>,
    pub command: Option<(Arg, Vec<Arg>)>,
}

impl Parsed {
    fn set(&mut self, dest: &'static str, v: Val) {
        self.vals.retain(|(d, _)| *d != dest);
        self.vals.push((dest, v));
    }
    fn get(&self, dest: &str) -> Option<&Val> {
        self.vals.iter().find(|(d, _)| *d == dest).map(|(_, v)| v)
    }
    pub fn string(&self, dest: &str) -> Option<Arg> {
        match self.get(dest) {
            Some(Val::Str(s)) => Some(s.clone()),
            _ => None,
        }
    }
    pub fn int(&self, dest: &str) -> Option<i64> {
        match self.get(dest) {
            Some(Val::Int(i)) => Some(*i),
            _ => None,
        }
    }
    pub fn float(&self, dest: &str) -> Option<f64> {
        match self.get(dest) {
            Some(Val::Float(f)) => Some(*f),
            _ => None,
        }
    }
    pub fn flag(&self, dest: &str) -> bool {
        matches!(self.get(dest), Some(Val::Flag))
    }
    pub fn list(&self, dest: &str) -> Vec<Arg> {
        match self.get(dest) {
            Some(Val::List(l)) => l.clone(),
            _ => Vec::new(),
        }
    }
}

/// Terminal outcome: text for stdout/stderr and the exit status.
pub struct Exit {
    pub code: i32,
    pub out: Vec<u8>,
    pub err: Vec<u8>,
}

impl Parser {
    pub fn error(&self, message: &str) -> Exit {
        Exit {
            code: 2,
            out: Vec::new(),
            err: format!("{}{}: error: {}\n", self.usage, self.prog, message).into_bytes(),
        }
    }
}

struct Tok {
    opt: Option<usize>,
    name: Arg,
    explicit: Option<Arg>,
}

fn negative_number(arg: &[u8]) -> bool {
    // ^-\d+$|^-\d*\.\d+$
    let Some(rest) = arg.strip_prefix(b"-") else { return false };
    let digits = |s: &[u8]| !s.is_empty() && s.iter().all(u8::is_ascii_digit);
    if digits(rest) {
        return true;
    }
    match rest.iter().position(|&b| b == b'.') {
        Some(i) => (i == 0 || digits(&rest[..i])) && digits(&rest[i + 1..]),
        None => false,
    }
}

impl Parser {
    fn exact(&self, arg: &[u8]) -> Option<usize> {
        self.opts
            .iter()
            .position(|o| o.names.iter().any(|n| n.as_bytes() == arg))
    }

    fn label(&self, opt: usize) -> String {
        self.opts[opt].names.join("/")
    }

    fn classify(&self, arg: &[u8]) -> Result<Option<Tok>, Exit> {
        if arg.first() != Some(&b'-') {
            return Ok(None);
        }
        if let Some(i) = self.exact(arg) {
            return Ok(Some(Tok { opt: Some(i), name: arg.to_vec(), explicit: None }));
        }
        if arg.len() == 1 {
            return Ok(None);
        }
        if let Some(eq) = arg.iter().position(|&b| b == b'=') {
            if let Some(i) = self.exact(&arg[..eq]) {
                return Ok(Some(Tok { opt: Some(i), name: arg[..eq].to_vec(), explicit: Some(arg[eq + 1..].to_vec()) }));
            }
        }
        let mut matches: Vec<(usize, &'static str, Option<Arg>)> = Vec::new();
        if arg.starts_with(b"--") {
            let (prefix, explicit) = match arg.iter().position(|&b| b == b'=') {
                Some(eq) => (&arg[..eq], Some(arg[eq + 1..].to_vec())),
                None => (arg, None),
            };
            for (i, o) in self.opts.iter().enumerate() {
                for n in o.names {
                    if n.as_bytes().starts_with(prefix) {
                        matches.push((i, n, explicit.clone()));
                    }
                }
            }
        } else {
            let (short, rest) = arg.split_at(2.min(arg.len()));
            for (i, o) in self.opts.iter().enumerate() {
                for n in o.names {
                    if n.as_bytes() == short {
                        matches.push((i, n, Some(rest.to_vec())));
                    } else if n.as_bytes().starts_with(arg) {
                        matches.push((i, n, None));
                    }
                }
            }
        }
        if matches.len() > 1 {
            let names: Vec<&str> = matches.iter().map(|m| m.1).collect();
            return Err(self.error(&format!(
                "ambiguous option: {} could match {}",
                String::from_utf8_lossy(arg),
                names.join(", ")
            )));
        }
        if let Some((i, name, explicit)) = matches.pop() {
            return Ok(Some(Tok { opt: Some(i), name: name.as_bytes().to_vec(), explicit }));
        }
        if negative_number(arg) || arg.contains(&b' ') {
            return Ok(None);
        }
        Ok(Some(Tok { opt: None, name: arg.to_vec(), explicit: None }))
    }

    fn convert(&self, opt: usize, value: &[u8]) -> Result<Val, Exit> {
        let o = &self.opts[opt];
        let bad = |kind: &str| self.error(&format!("argument {}: invalid {} value: {}", self.label(opt), kind, py::repr(value)));
        match &o.kind {
            Kind::Str => Ok(Val::Str(value.to_vec())),
            Kind::Int => py::parse_int(value).map(Val::Int).ok_or_else(|| bad("int")),
            Kind::Float => py::parse_float(value).map(Val::Float).ok_or_else(|| bad("float")),
            Kind::Choice(choices) => {
                if choices.iter().any(|c| c.as_bytes() == value) {
                    Ok(Val::Str(value.to_vec()))
                } else {
                    let listed: Vec<String> = choices.iter().map(|c| py::repr(c.as_bytes())).collect();
                    Err(self.error(&format!(
                        "argument {}: invalid choice: {} (choose from {})",
                        self.label(opt),
                        py::repr(value),
                        listed.join(", ")
                    )))
                }
            }
            Kind::Flag | Kind::Help => unreachable!(),
        }
    }

    /// Parses `args`; `Err` carries the help text or usage error to emit.
    pub fn parse(&self, args: &[Arg]) -> Result<Parsed, Exit> {
        let mut pattern = Vec::with_capacity(args.len());
        let mut toks: Vec<Option<Tok>> = Vec::with_capacity(args.len());
        let mut after_marker = false;
        for arg in args {
            if after_marker {
                pattern.push(b'A');
                toks.push(None);
            } else if arg == b"--" {
                pattern.push(b'-');
                toks.push(None);
                after_marker = true;
            } else {
                let tok = self.classify(arg)?;
                pattern.push(if tok.is_some() { b'O' } else { b'A' });
                toks.push(tok);
            }
        }
        let mut parsed = Parsed::default();
        let mut positional_done = false;
        let mut start = 0;
        let last_option = pattern.iter().rposition(|&c| c == b'O');
        if let Some(last) = last_option {
            while start <= last {
                let next = (start..pattern.len()).find(|&i| pattern[i] == b'O').unwrap();
                if start != next {
                    let end = self.consume_positionals(&mut parsed, &mut positional_done, args, &pattern, start)?;
                    if end > start {
                        start = end;
                        continue;
                    }
                }
                if pattern[start] != b'O' {
                    parsed.extras.extend(args[start..next].iter().cloned());
                    start = next;
                }
                start = self.consume_optional(&mut parsed, args, &pattern, &toks, start)?;
            }
        }
        let stop = self.consume_positionals(&mut parsed, &mut positional_done, args, &pattern, start)?;
        parsed.extras.extend(args[stop..].iter().cloned());
        if !positional_done && !matches!(self.pos, Pos::None) {
            let name = match self.pos {
                Pos::None => unreachable!(),
                Pos::One(d) => d,
                Pos::Remainder(d) => d,
                Pos::Command(_) => "operation",
            };
            return Err(self.error(&format!("the following arguments are required: {}", name)));
        }
        Ok(parsed)
    }

    fn consume_positionals(
        &self,
        parsed: &mut Parsed,
        done: &mut bool,
        args: &[Arg],
        pattern: &[u8],
        start: usize,
    ) -> Result<usize, Exit> {
        if *done || matches!(self.pos, Pos::None) {
            return Ok(start);
        }
        let rest = &pattern[start..];
        // Leading single marker allowed before the argument for One and Command.
        let lead = usize::from(rest.first() == Some(&b'-'));
        let count = match self.pos {
            Pos::None => unreachable!(),
            Pos::Remainder(_) => Some(rest.len()),
            Pos::One(_) => (rest.get(lead) == Some(&b'A')).then_some(lead + 1),
            Pos::Command(_) => (rest.get(lead) == Some(&b'A')).then_some(rest.len()),
        };
        let Some(count) = count else { return Ok(start) };
        *done = true;
        let mut taken: Vec<Arg> = args[start..start + count].to_vec();
        match &self.pos {
            Pos::None => unreachable!(),
            Pos::Remainder(dest) => parsed.set(dest, Val::List(taken)),
            Pos::One(dest) => {
                if let Some(i) = taken.iter().position(|a| a == b"--") {
                    taken.remove(i);
                }
                parsed.set(dest, Val::Str(taken.remove(0)));
            }
            Pos::Command(choices) => {
                let name = taken.remove(0);
                if !choices.iter().any(|c| c.as_bytes() == name.as_slice()) {
                    let listed: Vec<String> = choices.iter().map(|c| py::repr(c.as_bytes())).collect();
                    return Err(self.error(&format!(
                        "argument operation: invalid choice: {} (choose from {})",
                        py::repr(&name),
                        listed.join(", ")
                    )));
                }
                parsed.command = Some((name, taken));
            }
        }
        Ok(start + count)
    }

    fn consume_optional(
        &self,
        parsed: &mut Parsed,
        args: &[Arg],
        pattern: &[u8],
        toks: &[Option<Tok>],
        index: usize,
    ) -> Result<usize, Exit> {
        let tok = toks[index].as_ref().unwrap();
        let Some(mut opt) = tok.opt else {
            parsed.extras.push(args[index].clone());
            return Ok(index + 1);
        };
        let mut actions: Vec<(usize, Option<Arg>)> = Vec::new();
        let mut explicit = tok.explicit.clone();
        let mut name = tok.name.clone();
        let stop;
        loop {
            let takes_value = !matches!(self.opts[opt].kind, Kind::Help | Kind::Flag);
            if let Some(e) = explicit.clone() {
                if takes_value {
                    actions.push((opt, Some(e)));
                    stop = index + 1;
                    break;
                }
                let single_dash = name.get(1) != Some(&b'-');
                if single_dash && !e.is_empty() {
                    actions.push((opt, None));
                    let next = [b'-', e[0]];
                    match self.exact(&next) {
                        Some(i) => {
                            opt = i;
                            name = next.to_vec();
                            explicit = (e.len() > 1).then(|| e[1..].to_vec());
                            continue;
                        }
                        None => {
                            return Err(self.error(&format!(
                                "argument {}: ignored explicit argument {}",
                                self.label(opt),
                                py::repr(&e)
                            )));
                        }
                    }
                }
                return Err(self.error(&format!(
                    "argument {}: ignored explicit argument {}",
                    self.label(opt),
                    py::repr(&e)
                )));
            }
            if takes_value {
                if pattern.get(index + 1) != Some(&b'A') {
                    return Err(self.error(&format!("argument {}: expected one argument", self.label(opt))));
                }
                actions.push((opt, Some(args[index + 1].clone())));
                stop = index + 2;
            } else {
                actions.push((opt, None));
                stop = index + 1;
            }
            break;
        }
        for (opt, value) in actions {
            match (&self.opts[opt].kind, value) {
                (Kind::Help, _) => {
                    return Err(Exit { code: 0, out: self.help.as_bytes().to_vec(), err: Vec::new() });
                }
                (Kind::Flag, _) => parsed.set(self.opts[opt].dest, Val::Flag),
                (_, Some(v)) => {
                    let converted = self.convert(opt, &v)?;
                    parsed.set(self.opts[opt].dest, converted);
                }
                _ => unreachable!(),
            }
        }
        Ok(stop)
    }
}

pub const TOP_USAGE: &str = "usage: run.py [-h] {run,inspect,prune} ...\n";
const TOP_HELP: &str = "usage: run.py [-h] {run,inspect,prune} ...

Capture command evidence before returning bounded diagnostics to an agent.

positional arguments:
  {run,inspect,prune}
    run                Capture complete output and return one JSON result;
                       noninteractive
    inspect            Bounded byte-addressed diagnostic expansion; offsets
                       allow pagination
    prune              Dry-run by default: old evidence not referenced by
                       tasks/active

options:
  -h, --help           show this help message and exit
";

const RUN_USAGE: &str = "usage: run.py run [-h] [--cwd CWD] [--timeout TIMEOUT] [--shell SHELL]
                  [--shell-executable SHELL_EXECUTABLE]
                  [--inline-bytes INLINE_BYTES] [--kill-after KILL_AFTER]
                  [--max-log-bytes MAX_LOG_BYTES]
                  ...
";
const RUN_HELP: &str = "usage: run.py run [-h] [--cwd CWD] [--timeout TIMEOUT] [--shell SHELL]
                  [--shell-executable SHELL_EXECUTABLE]
                  [--inline-bytes INLINE_BYTES] [--kill-after KILL_AFTER]
                  [--max-log-bytes MAX_LOG_BYTES]
                  ...

positional arguments:
  command

options:
  -h, --help            show this help message and exit
  --cwd CWD
  --timeout TIMEOUT
  --validation
                        Require an independent argv check (reject --shell)
  --shell SHELL
                        Fail fast with errexit and pipefail (bash/zsh)
  --shell-keep-going
                        Explicit legacy shell semantics; not for validations
  --shell-executable SHELL_EXECUTABLE
  --inline-bytes INLINE_BYTES
                        Inline stdout/stderr when their total size is at most
                        this (0 disables)
  --kill-after KILL_AFTER
                        Seconds between SIGTERM and SIGKILL (lets sims
                        finalize MP4/HDF5)
  --max-log-bytes MAX_LOG_BYTES
                        Stop the command (status LOG_LIMIT, exit 125) when one
                        stream exceeds this
";
const INSPECT_USAGE: &str = "usage: run.py inspect [-h] [--stream {auto,stdout,stderr,stdout.log,stderr.log}] [--offset OFFSET]
                      [--lines LINES] [--max-bytes MAX_BYTES]
                      [--search SEARCH]
                      evidence
";
const INSPECT_HELP: &str = "usage: run.py inspect [-h] [--stream {auto,stdout,stderr,stdout.log,stderr.log}] [--offset OFFSET]
                      [--lines LINES] [--max-bytes MAX_BYTES]
                      [--search SEARCH]
                      evidence

positional arguments:
  evidence

options:
  -h, --help            show this help message and exit
  --stream {auto,stdout,stderr,stdout.log,stderr.log}
  --offset OFFSET
  --lines LINES
  --max-bytes MAX_BYTES
  --search SEARCH       Literal substring filter; use short distinctive terms
";
const PRUNE_USAGE: &str = "usage: run.py prune [-h] [--days DAYS] [--apply]\n";
const PRUNE_HELP: &str = "usage: run.py prune [-h] [--days DAYS] [--apply]

options:
  -h, --help   show this help message and exit
  --days DAYS
  --apply
";

const HELP: Opt = Opt { names: &["-h", "--help"], dest: "help", kind: Kind::Help };

pub static TOP: Parser = Parser {
    prog: "run.py",
    usage: TOP_USAGE,
    help: TOP_HELP,
    opts: &[HELP],
    pos: Pos::Command(&["run", "inspect", "prune"]),
};

pub static RUN: Parser = Parser {
    prog: "run.py run",
    usage: RUN_USAGE,
    help: RUN_HELP,
    opts: &[
        HELP,
        Opt { names: &["--cwd"], dest: "cwd", kind: Kind::Str },
        Opt { names: &["--timeout"], dest: "timeout", kind: Kind::Float },
        Opt { names: &["--validation"], dest: "validation", kind: Kind::Flag },
        Opt { names: &["--shell"], dest: "shell", kind: Kind::Str },
        Opt { names: &["--shell-keep-going"], dest: "shell_keep_going", kind: Kind::Flag },
        Opt { names: &["--shell-executable"], dest: "shell_executable", kind: Kind::Str },
        Opt { names: &["--inline-bytes"], dest: "inline_bytes", kind: Kind::Int },
        Opt { names: &["--kill-after"], dest: "kill_after", kind: Kind::Float },
        Opt { names: &["--max-log-bytes"], dest: "max_log_bytes", kind: Kind::Int },
    ],
    pos: Pos::Remainder("command"),
};

pub static INSPECT: Parser = Parser {
    prog: "run.py inspect",
    usage: INSPECT_USAGE,
    help: INSPECT_HELP,
    opts: &[
        HELP,
        Opt { names: &["--stream"], dest: "stream", kind: Kind::Choice(&["auto", "stdout", "stderr", "stdout.log", "stderr.log"]) },
        Opt { names: &["--offset"], dest: "offset", kind: Kind::Int },
        Opt { names: &["--lines"], dest: "lines", kind: Kind::Int },
        Opt { names: &["--max-bytes"], dest: "max_bytes", kind: Kind::Int },
        Opt { names: &["--search"], dest: "search", kind: Kind::Str },
    ],
    pos: Pos::One("evidence"),
};

pub static PRUNE: Parser = Parser {
    prog: "run.py prune",
    usage: PRUNE_USAGE,
    help: PRUNE_HELP,
    opts: &[
        HELP,
        Opt { names: &["--days"], dest: "days", kind: Kind::Float },
        Opt { names: &["--apply"], dest: "apply", kind: Kind::Flag },
    ],
    pos: Pos::None,
};

/// Parses a full argv (after the program name), dispatching the subcommand.
pub fn parse_command(args: &[Arg]) -> Result<(&'static str, &'static Parser, Parsed), Exit> {
    let top = TOP.parse(args)?;
    let (name, rest) = top.command.clone().expect("command is required");
    let (label, parser): (&'static str, &'static Parser) = match name.as_slice() {
        b"run" => ("run", &RUN),
        b"inspect" => ("inspect", &INSPECT),
        _ => ("prune", &PRUNE),
    };
    let mut sub = parser.parse(&rest)?;
    // Subcommand-only extras report the subcommand usage, which lists its accepted options.
    let reporter = if top.extras.is_empty() { parser } else { &TOP };
    let mut extras = top.extras;
    extras.append(&mut sub.extras);
    if !extras.is_empty() {
        let joined: Vec<String> = extras.iter().map(|e| String::from_utf8_lossy(e).into_owned()).collect();
        return Err(reporter.error(&format!("unrecognized arguments: {}", joined.join(" "))));
    }
    Ok((label, parser, sub))
}
