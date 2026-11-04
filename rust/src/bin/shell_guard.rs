//! PreToolUse hook: require the evidence wrapper without granting native execution permission.
//!
//! Only the native `run` executor (run/inspect/prune) and `local-read` pass; every other
//! shell command is denied. Stdout is the compact JSON of Python's
//! `json.dumps(result, separators=(',', ':'))`. The JSON reader, `shlex` and `str.strip`
//! follow CPython 3.12 so that fail-closed reasons stay stable
//! (see `tools/test_shell_guard.py` and its recorded fixture).

use regex::bytes::{Regex as ByteRegex, RegexSet};
use std::io::{Read, Write};

const GUIDANCE_TAIL: &str = "Shell output must use the evidence wrapper. Invoke canonical shlex.join(argv): \
{NATIVE} run [--cwd DIR] [--timeout S] -- COMMAND ARGS (or inspect ARTIFACT). \
For shell syntax pass one single-quoted --shell 'SCRIPT' argument; no outer operators, \
redirection or cd (use --cwd).";

/// CPython's recursion headroom for the C JSON scanner (see `Parser::fail`).
const MAX_OPEN_CONTAINERS: usize = 9997;
/// Python refuses `int()` of more decimal digits than this.
const MAX_INT_DIGITS: usize = 4300;

// ---------------------------------------------------------------- policy

const GIT: &str = r"\bgit(?:\s+-[Cc]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+";
const ARGS: &str = r"(?:[^\s;&|]+\s+)*";

fn deny_patterns() -> Vec<String> {
    vec![
        r"\bsudo\b|\bsu\s+-".to_string(),
        format!(r"{GIT}push\s+{ARGS}(?:-f|--force)(?:\s|$)"),
        format!(r"\brm\s+{ARGS}-[a-zA-Z]*(?:[rR][a-zA-Z]*f|f[a-zA-Z]*[rR])"),
        format!(r"\bmkfs(?:\.\w+)?\b|\bdd\s+{ARGS}of=/dev/"),
    ]
}

const DENY_LABELS: [&str; 4] = ["privilege escalation", "git force push", "rm -rf", "raw device write"];

fn ask_patterns() -> Vec<String> {
    vec![
        r#"(?:^|[\s'"/])(?:\.ssh|\.aws|\.gnupg)(?:/|[\s'"$])|(?:^|[\s'"/])\.env(?:[.\s'"/]|$)|(?:^|[\s'"/])(?:id_rsa|id_ed25519|credentials)(?:[.\s'"/]|$)"#.to_string(),
        format!(r"{GIT}(?:push|commit|rebase|filter-branch)\b"),
        format!(r"{GIT}(?:reset\s+--hard|clean\s+-\w*f|branch\s+-D|checkout\s+--\s|restore\b)"),
        format!(r"\brm\s+{ARGS}(?:-[a-zA-Z]*[rR]|--recursive)\b"),
        r"\b(?:ssh|scp|sftp|rsync)\b".to_string(),
        format!(
            r"\bcurl\s+{ARGS}(?:-T|--upload-file|-d|--data[\w-]*|-F|--form|-X\s*(?:POST|PUT|PATCH|DELETE))\b|\bwget\s+{ARGS}--post"
        ),
        r"\bros2\s+(?:topic\s+pub|service\s+call|action\s+send_goal|param\s+set|lifecycle\s+set|launch|run|bag\s+play)\b|\bz_(?:put|pub|delete|queryable)\b".to_string(),
        r#"(?:^|[\s;&|(`])(?:env|printenv|set)\s*(?:$|[;&|)`])|\bprintenv\b|\bexport\s+-p\b|(?:^|[\s/'"=])(?:\.env(?:\.\w+)?|auth\.json|[\w.-]*\.key|id_(?:rsa|ed25519))\b"#.to_string(),
    ]
}

const ASK_LABELS: [&str; 8] = [
    "sensitive credential access",
    "git history or remote change",
    "git destructive change",
    "recursive delete",
    "remote host access (robot or server)",
    "network upload",
    "possible robot actuation",
    "possible secret exposure",
];

/// Python 3.12's non-ASCII `\w` (`str.isalnum()`): letters and numbers assigned as of Unicode 15.0
/// (verified identical to CPython 3.12 over every non-ASCII code point).
const WORD_CLASS: &str = r"^[\p{L}\p{N}&&\p{Age=V15_0}]$";

/// Python `str.isspace()` for one character (Unicode whitespace plus U+001C..U+001F).
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Rewrite text to pure ASCII while preserving, for every character, exactly what
/// Python's Unicode `\w`/`\s`/`\b` see. Patterns only contain ASCII literals, so a
/// non-ASCII character matters only as "word", "space" or "other"; they become `0`,
/// ` ` and `#` (neither occurs as a literal in any pattern), and the patterns can
/// then use cheap ASCII-only classes.
fn normalize(text: &str) -> Vec<u8> {
    let mut word_class: Option<ByteRegex> = None;
    let mut out = Vec::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(if ('\u{1c}'..='\u{1f}').contains(&c) { b' ' } else { c as u8 });
        } else if py_isspace(c) {
            out.push(b' ');
        } else {
            // Python \w: str.isalnum() = letters (L*) and numeric (N*) characters.
            let regex = word_class
                .get_or_insert_with(|| ByteRegex::new(WORD_CLASS).unwrap_or_else(|_| std::process::abort()));
            let mut buf = [0u8; 4];
            let encoded = c.encode_utf8(&mut buf).as_bytes();
            out.push(if regex.is_match(encoded) { b'0' } else { b'#' });
        }
    }
    out
}

fn first_match(patterns: &[String], text: &[u8]) -> Option<usize> {
    let patterns: Vec<String> = patterns.iter().map(|p| format!("(?m-u){p}")).collect();
    let set = RegexSet::new(&patterns).ok()?;
    set.matches(text).iter().next()
}

/// `(kind, reason)` of a policy hit, or `None` for pass.
fn policy(text: &str) -> Option<Decision> {
    let text = normalize(text);
    if let Some(index) = first_match(&deny_patterns(), &text) {
        return Some(Decision::new(
            "deny",
            format!("Blocked by agent policy: {}. Ask the user to run it themselves.", DENY_LABELS[index]),
        ));
    }
    first_match(&ask_patterns(), &text).map(|index| {
        Decision::new("ask", format!("Agent policy requires explicit user approval: {}.", ASK_LABELS[index]))
    })
}

// ---------------------------------------------------------------- shlex

fn is_shlex_whitespace(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\r' | '\n')
}

#[derive(Clone, Copy)]
enum Lex {
    Space,
    Word,
    Single,
    Double,
    Escape { in_double: bool },
}

/// `shlex.split(s, posix=True)`; `None` is Python's `ValueError`.
fn shlex_split(text: &str) -> Option<Vec<String>> {
    let mut chars = text.chars();
    let mut tokens = Vec::new();
    loop {
        let mut state = Lex::Space;
        let mut token = String::new();
        let mut quoted = false;
        let at_eof = loop {
            let next = chars.next();
            match state {
                Lex::Space => match next {
                    None => break true,
                    Some(c) if is_shlex_whitespace(c) => {}
                    Some('\\') => state = Lex::Escape { in_double: false },
                    Some('\'') => state = Lex::Single,
                    Some('"') => state = Lex::Double,
                    Some(c) => {
                        token.push(c);
                        state = Lex::Word;
                    }
                },
                Lex::Word => match next {
                    None => break true,
                    Some(c) if is_shlex_whitespace(c) => {
                        if !token.is_empty() || quoted {
                            break false;
                        }
                        state = Lex::Space;
                    }
                    Some('\'') => state = Lex::Single,
                    Some('"') => state = Lex::Double,
                    Some('\\') => state = Lex::Escape { in_double: false },
                    Some(c) => token.push(c),
                },
                Lex::Single => {
                    quoted = true;
                    match next {
                        None => return None,
                        Some('\'') => state = Lex::Word,
                        Some(c) => token.push(c),
                    }
                }
                Lex::Double => {
                    quoted = true;
                    match next {
                        None => return None,
                        Some('"') => state = Lex::Word,
                        Some('\\') => state = Lex::Escape { in_double: true },
                        Some(c) => token.push(c),
                    }
                }
                Lex::Escape { in_double } => match next {
                    None => return None,
                    Some(c) => {
                        if in_double && c != '\\' && c != '"' {
                            token.push('\\');
                        }
                        token.push(c);
                        state = if in_double { Lex::Double } else { Lex::Word };
                    }
                },
            }
        };
        if !token.is_empty() || quoted {
            tokens.push(token);
        }
        if at_eof {
            return Some(tokens);
        }
    }
}

/// `shlex.quote`: safe set is `[A-Za-z0-9_@%+=:,./-]`.
fn shlex_quote(arg: &str) -> String {
    if arg.is_empty() {
        return "''".to_string();
    }
    let safe = |c: char| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c);
    if arg.chars().all(safe) {
        return arg.to_string();
    }
    format!("'{}'", arg.replace('\'', "'\"'\"'"))
}

fn shlex_join(argv: &[String]) -> String {
    argv.iter().map(|arg| shlex_quote(arg)).collect::<Vec<_>>().join(" ")
}

// ---------------------------------------------------------------- json

enum Json {
    Null,
    Bool,
    Number,
    Str(String),
    Array,
    Object(Vec<(String, Json)>),
}

impl Json {
    /// Python dict lookup: the last duplicate key wins.
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

enum Fail {
    /// `ValueError` / `UnicodeError`.
    Invalid,
    /// `RecursionError` out of the C scanner.
    Recursion,
}

/// Mirrors `json.loads` (strict, CPython 3.12 C scanner), including NaN/Infinity and
/// lone surrogates (kept as U+FFFD: they are neither word nor space characters and
/// the guard never echoes input).
struct Parser<'a> {
    text: &'a str,
    pos: usize,
    open: usize,
}

impl<'a> Parser<'a> {
    fn bytes(&self) -> &'a [u8] {
        self.text.as_bytes()
    }

    fn peek(&self) -> Option<u8> {
        self.bytes().get(self.pos).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    /// A syntax error raised while `open` containers are active. CPython builds the
    /// `JSONDecodeError` with Python calls that need stack headroom, so deep inputs
    /// surface `RecursionError` instead; the margin depends on the error kind.
    fn fail(&self, expecting_value: bool) -> Fail {
        let margin = if expecting_value { 0 } else { 3 };
        if self.open + margin >= MAX_OPEN_CONTAINERS {
            Fail::Recursion
        } else {
            Fail::Invalid
        }
    }

    fn literal(&mut self, word: &str, value: Json) -> Result<Json, Fail> {
        if self.bytes()[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(self.fail(true))
        }
    }

    fn enter(&mut self) -> Result<(), Fail> {
        self.open += 1;
        if self.open > MAX_OPEN_CONTAINERS {
            return Err(Fail::Recursion);
        }
        Ok(())
    }

    fn value(&mut self) -> Result<Json, Fail> {
        match self.peek() {
            Some(b'"') => self.string().map(Json::Str),
            Some(b'{') => {
                self.enter()?;
                let result = self.object();
                self.open -= 1;
                result
            }
            Some(b'[') => {
                self.enter()?;
                let result = self.array();
                self.open -= 1;
                result
            }
            Some(b'n') => self.literal("null", Json::Null),
            Some(b't') => self.literal("true", Json::Bool),
            Some(b'f') => self.literal("false", Json::Bool),
            Some(b'N') => self.literal("NaN", Json::Number),
            Some(b'I') => self.literal("Infinity", Json::Number),
            Some(b'-') if self.bytes()[self.pos..].starts_with(b"-Infinity") => {
                self.pos += "-Infinity".len();
                Ok(Json::Number)
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.fail(true)),
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.pos;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        self.pos - start
    }

    fn number(&mut self) -> Result<Json, Fail> {
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let int_digits = match self.peek() {
            Some(b'0') => {
                self.pos += 1;
                1
            }
            Some(b'1'..=b'9') => self.digits(),
            _ => return Err(self.fail(true)),
        };
        let mut is_float = false;
        let digit_at = |parser: &Self, offset: usize| matches!(parser.bytes().get(parser.pos + offset), Some(b'0'..=b'9'));
        if self.peek() == Some(b'.') && digit_at(self, 1) {
            self.pos += 1;
            self.digits();
            is_float = true;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let sign = usize::from(matches!(self.bytes().get(self.pos + 1), Some(b'+' | b'-')));
            if digit_at(self, 1 + sign) {
                self.pos += 1 + sign;
                self.digits();
                is_float = true;
            }
        }
        if !is_float && int_digits > MAX_INT_DIGITS {
            return Err(Fail::Invalid);
        }
        Ok(Json::Number)
    }

    fn hex4(&self, at: usize) -> Option<u32> {
        let digits = self.bytes().get(at..at + 4)?;
        digits.iter().try_fold(0u32, |acc, &b| Some(acc * 16 + char::from(b).to_digit(16)?))
    }

    fn string(&mut self) -> Result<String, Fail> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        loop {
            let start = self.pos;
            while matches!(self.peek(), Some(b) if b != b'"' && b != b'\\' && b >= 0x20) {
                self.pos += 1;
            }
            out.push_str(self.text.get(start..self.pos).ok_or(Fail::Invalid)?);
            match self.peek() {
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {}
                _ => return Err(self.fail(false)), // unterminated or control character
            }
            let escape = *self.bytes().get(self.pos + 1).ok_or_else(|| self.fail(false))?;
            self.pos += 2;
            let c = match escape {
                b'"' => '"',
                b'\\' => '\\',
                b'/' => '/',
                b'b' => '\u{8}',
                b'f' => '\u{c}',
                b'n' => '\n',
                b'r' => '\r',
                b't' => '\t',
                b'u' => {
                    let mut unit = self.hex4(self.pos).ok_or_else(|| self.fail(false))?;
                    self.pos += 4;
                    if (0xd800..0xdc00).contains(&unit) && self.bytes()[self.pos..].starts_with(b"\\u") {
                        let low = self.hex4(self.pos + 2).ok_or_else(|| self.fail(false))?;
                        if (0xdc00..0xe000).contains(&low) {
                            unit = 0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00);
                            self.pos += 6;
                        }
                    }
                    char::from_u32(unit).unwrap_or('\u{fffd}')
                }
                _ => return Err(self.fail(false)),
            };
            out.push(c);
        }
    }

    fn array(&mut self) -> Result<Json, Fail> {
        self.pos += 1;
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array);
        }
        loop {
            self.value()?;
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                    self.skip_whitespace();
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Array);
                }
                _ => return Err(self.fail(false)),
            }
        }
    }

    fn object(&mut self) -> Result<Json, Fail> {
        self.pos += 1;
        self.skip_whitespace();
        let mut pairs = Vec::new();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(pairs));
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(self.fail(false));
            }
            let key = self.string()?;
            self.skip_whitespace();
            if self.peek() != Some(b':') {
                return Err(self.fail(false));
            }
            self.pos += 1;
            self.skip_whitespace();
            pairs.push((key, self.value()?));
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                    self.skip_whitespace();
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Json::Object(pairs));
                }
                _ => return Err(self.fail(false)),
            }
        }
    }
}

fn parse_json(text: &str) -> Result<Json, Fail> {
    let mut parser = Parser { text, pos: 0, open: 0 };
    parser.skip_whitespace();
    let value = parser.value()?;
    parser.skip_whitespace();
    if parser.pos != text.len() {
        return Err(Fail::Invalid);
    }
    Ok(value)
}

/// `json.dumps` string with `ensure_ascii=True`.
fn json_string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------- guard

struct Decision {
    kind: &'static str,
    reason: String,
}

impl Decision {
    fn new(kind: &'static str, reason: String) -> Self {
        Decision { kind, reason }
    }

    fn render(decision: Option<&Decision>) -> String {
        let Some(decision) = decision else { return "{}".to_string() };
        let mut out = String::from(r#"{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"#);
        json_string(decision.kind, &mut out);
        out.push_str(r#","permissionDecisionReason":"#);
        json_string(&decision.reason, &mut out);
        out.push_str("}}");
        out
    }
}

struct Guard {
    native: String,
    local_read: String,
    guidance: String,
}

/// `os.path.expanduser('~' + rest)`.
fn expanduser(rest: &str) -> String {
    let home = match std::env::var_os("HOME") {
        Some(home) => home.to_string_lossy().into_owned(),
        None => {
            // SAFETY: getpwuid returns NULL or a pointer to static storage valid until the next call.
            let entry = unsafe { libc::getpwuid(libc::getuid()) };
            if entry.is_null() {
                return format!("~{rest}");
            }
            // SAFETY: non-null passwd entry; pw_dir is a NUL-terminated string.
            unsafe { std::ffi::CStr::from_ptr((*entry).pw_dir) }.to_string_lossy().into_owned()
        }
    };
    let home = home.trim_end_matches('/');
    format!("{home}{rest}")
}

impl Guard {
    fn new() -> Self {
        let native = expanduser("/.agents/tools/robot-exec/native/run");
        let local_read = expanduser("/.agents/tools/robot-exec/local-read");
        let guidance = GUIDANCE_TAIL.replace("{NATIVE}", &native);
        Guard { native, local_read, guidance }
    }

    fn denied(&self, reason: &str) -> Option<Decision> {
        Some(Decision::new("deny", format!("{reason} {}", self.guidance)))
    }

    /// The command the wrapper will execute, as text for policy matching.
    fn inner_text(argv: &[String]) -> String {
        if argv[1] != "run" {
            return String::new();
        }
        if let Some(index) = argv.iter().position(|a| a == "--shell") {
            if let Some(script) = argv.get(index + 1) {
                return script.clone();
            }
        }
        match argv.iter().position(|a| a == "--") {
            Some(index) => shlex_join(&argv[index + 1..]),
            None => shlex_join(&argv[2..]),
        }
    }

    fn evaluate(&self, payload: &Json) -> Option<Decision> {
        let Some(Json::Str(tool)) = payload.get("tool_name") else {
            return self.denied("Invalid hook input.");
        };
        if !matches!(tool.as_str(), "Bash" | "exec_command" | "shell_command") {
            return None;
        }
        let tool_input = payload.get("tool_input");
        let command = match tool_input {
            Some(input @ Json::Object(_)) => input.get("command").or_else(|| input.get("cmd")),
            _ => None,
        };
        let command = match command {
            Some(Json::Str(command)) if !command.trim_matches(py_isspace).is_empty() => command,
            _ => return self.denied("Missing shell command."),
        };
        let Some(argv) = shlex_split(command) else {
            return self.denied("Malformed shell quoting.");
        };
        if argv.is_empty() || command.trim_matches(py_isspace) != shlex_join(&argv) {
            return self.denied("Unwrapped or noncanonical shell command.");
        }
        if argv[0] == self.local_read {
            let allowed: &[usize] = match argv.get(1).map(String::as_str) {
                Some("status" | "files") => &[2],
                Some("text") => &[3, 4, 5],
                Some("find") => &[4],
                _ => &[],
            };
            if !allowed.contains(&argv.len()) {
                return self.denied("Unsupported read capability or arguments.");
            }
            // The compiled launcher validates roots, sensitive paths and numeric ranges.
            // Empty output preserves native sandbox/permission checks; never grant allow.
            return None;
        }
        // Canonical quoting makes metacharacters literal arguments to the wrapper.
        // Never return "allow": native sandbox and approval checks still apply.
        if argv.len() < 2 || argv[0] != self.native || !matches!(argv[1].as_str(), "run" | "inspect" | "prune") {
            return self.denied("Unwrapped or noncanonical shell command.");
        }
        if argv.iter().any(|arg| arg.starts_with('=')) {
            // zsh EQUALS expansion rewrites an unquoted "=cmd" into a path.
            return self.denied("Argument starting with \"=\" is expanded by zsh.");
        }
        if argv[1] == "prune" && argv.iter().any(|a| a == "--apply") {
            return Some(Decision::new("ask", "Deleting old evidence requires explicit user approval.".to_string()));
        }
        policy(&Self::inner_text(&argv))
    }
}

fn run() -> String {
    let guard = Guard::new();
    let mut input = Vec::new();
    if std::io::stdin().read_to_end(&mut input).is_err() {
        return Decision::render(guard.denied("Guard internal error (OSError).").as_ref());
    }
    let parsed = std::str::from_utf8(&input).map_err(|_| Fail::Invalid).and_then(parse_json);
    let decision = match parsed {
        Ok(payload) => guard.evaluate(&payload),
        Err(Fail::Invalid) => guard.denied("Invalid hook JSON."),
        Err(Fail::Recursion) => guard.denied("Guard internal error (RecursionError)."),
    };
    Decision::render(decision.as_ref())
}

fn main() {
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{}", run());
    let _ = stdout.flush();
}
