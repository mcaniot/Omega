//! PreToolUse hook: require the evidence wrapper without granting native execution permission.
//!
//! With `--allow-clean` (Claude only), a wrapper call that passes every check and matches no
//! deny/ask policy returns `allow` instead of deferring to native permission prompts.
//!
//! Only the native executor (`run`, `inspect`, `prune` or the `omega` CLI) passes; every other
//! shell command is denied or auto-wrapped in `Ω-run --shell`. Stdout is the compact JSON of Python's
//! `json.dumps(result, separators=(',', ':'))`. The JSON reader, `shlex` and `str.strip`
//! follow CPython 3.12 so that fail-closed reasons stay stable
//! (see `tools/test_shell_guard.py` and its recorded fixture).

use regex::bytes::{Regex as ByteRegex, RegexSet};
use std::io::{Read, Write};

const GUIDANCE_TAIL: &str = "Shell output must use the evidence wrapper. Invoke canonical shlex.join(argv): \
{NATIVE} run [--cwd DIR] [--timeout S] -- COMMAND ARGS (or inspect ARTIFACT). \
For shell syntax pass one single-quoted --shell 'SCRIPT' argument; no outer operators, \
redirection or cd (use --cwd).";

/// Names of the native `omega` CLI: plain commands, not output wrappers.
const OMEGA_CLI: [&str; 5] = ["omega", "omega-optimize", "\u{3a9}-optimize", "omega-resume", "\u{3a9}-resume"];

/// Longest suggested rewrite; keeps hook output bounded.
const MAX_REWRITE_BYTES: usize = 300;

/// Longest command run through the wrapper on the model's behalf (heredoc scripts included).
const MAX_AUTO_WRAP_BYTES: usize = 16 * 1024;

/// CPython's recursion headroom for the C JSON scanner (see `Parser::fail`).
const MAX_OPEN_CONTAINERS: usize = 9997;
/// Python refuses `int()` of more decimal digits than this.
const MAX_INT_DIGITS: usize = 4300;

// ---------------------------------------------------------------- policy

const GIT: &str = r"\bgit(?:\s+-[Cc]\s+\S+|\s+-[pP]|\s+--[\w-]+(?:=\S+|\s+[^\s-]\S*)?)*\s+";
const ARGS: &str = r"(?:[^\s;&|]+\s+)*";

fn deny_patterns() -> Vec<String> {
    vec![
        r"\bsudo\b|\bsu\s+-".to_string(),
        format!(r"{GIT}push\s+{ARGS}(?:-f|--force)(?:\s|$)"),
        // Any rm invocation (also /bin/rm, xargs rm, -- rm); not --rm flags. `git rm` is masked in `policy`.
        r"(?:^|[^\w.\-])rm(?:\s|$)".to_string(),
        format!(r"\bmkfs(?:\.\w+)?\b|\bdd\s+{ARGS}of=/dev/"),
        // `pkill -f PATTERN` with nothing (`[x]`, `^`) keeping the pattern from matching its own command line.
        // Command position only (script or line start, after `;`, `&`, `|`, `(`), so prose naming it passes.
        format!(r"(?:^|[;&|(])\s*pkill\s+{ARGS}(?:-[a-zA-Z0-9]*f[a-zA-Z0-9]*|--full)\s+[^;&|\[\^\n]*(?:[;&|]|$)"),
    ]
}

const DENY_LABELS: [&str; 5] = ["privilege escalation", "git force push", "rm", "raw device write", "pkill -f self-match"];
const DENY_HINTS: [&str; 5] = [
    "Ask the user to run it themselves.",
    "Ask the user to run it themselves.",
    "Delete with trash-put PATH... (recoverable via trash-restore); never rm.",
    "Ask the user to run it themselves.",
    "The pattern also matches the calling shell and the wrapper, which die with it. Kill the PIDs listed by pgrep, or bracket one letter: pkill -f '[n]ame'.",
];

fn ask_patterns() -> Vec<String> {
    vec![
        r#"(?:^|[\s'"/])(?:\.ssh|\.aws|\.gnupg)(?:/|[\s'"$]|$)|(?:^|[\s'"/])\.env(?:[.\s'"/]|$)|(?:^|[\s'"/])(?:id_rsa|id_ed25519|credentials)(?:[.\s'"/]|$)"#.to_string(),
        format!(r"{GIT}(?:push|commit|rebase|filter-branch)\b"),
        format!(r#"{GIT}(?:reset\s+--hard|clean\s+-\w*f|branch\s+-D|checkout\s+--\s|restore\b|stash(?:\s+(?:-|push|save|pop|apply|drop|clear)|\s*(?:$|[;&|'"])))"#),
        format!(r"\brm\s+{ARGS}(?:-[a-zA-Z]*[rR]|--recursive)\b"),
        r"\b(?:ssh|scp|sftp|rsync)\b".to_string(),
        format!(
            r"\bcurl\s+{ARGS}(?:-T|--upload-file|-d|--data[\w-]*|-F|--form|-X\s*(?:POST|PUT|PATCH|DELETE))\b|\bwget\s+{ARGS}--post"
        ),
        r"\bros2\s+(?:topic\s+pub|service\s+call|action\s+send_goal|param\s+set|lifecycle\s+set|launch|run|bag\s+play)\b|\bz_(?:put|pub|delete|queryable)\b".to_string(),
        r#"(?:^|[\s;&|(`])(?:env|printenv|set)\s*(?:$|[;&|)`])|\bprintenv\b|\bexport\s+-p\b|(?:^|[\s/'"=])(?:\.env(?:\.\w+)?|auth\.json|[\w.-]*\.key|id_(?:rsa|ed25519))\b"#.to_string(),
        // Nested `Ω-prune --apply` / `NATIVE prune --apply` (any `--a…` prefix); also matches a
        // hypothetical `git prune --apply`, which does not exist.
        format!(r#"\bprune['"]?\s+{ARGS}--a(?:p(?:p(?:ly?)?)?)?(?:[\s='"]|$)"#),
    ]
}

const ASK_LABELS: [&str; 9] = [
    "sensitive credential access",
    "git history or remote change",
    "git destructive change",
    "recursive delete",
    "remote host access (robot or server)",
    "network upload",
    "possible robot actuation",
    "possible secret exposure",
    "evidence deletion",
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
    let set = RegexSet::new(&patterns).unwrap_or_else(|_| std::process::abort());
    set.matches(text).iter().next()
}

/// The deny or ask `Decision` of a policy hit, or `None` for pass.
fn policy(text: &str) -> Option<Decision> {
    let text = normalize(text);
    // `git rm` is versioned (recoverable); keep it out of the rm deny rule only.
    let git_rm = ByteRegex::new(&format!(r"(?-u){GIT}rm\b")).unwrap_or_else(|_| std::process::abort());
    // A newline or separator ends the git command: what follows is not its subcommand.
    let masked = git_rm.replace_all(&text, |m: &regex::bytes::Captures| {
        if m[0].iter().any(|b| b"\n;&|".contains(b)) { m[0].to_vec() } else { b"git x".to_vec() }
    });
    if let Some(index) = first_match(&deny_patterns(), &masked) {
        return Some(Decision::new(
            "deny",
            format!("Blocked by agent policy: {}. {}", DENY_LABELS[index], DENY_HINTS[index]),
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

/// The command without its heredoc bodies and their delimiter lines; `None` without a heredoc or
/// when a body is not terminated. `<<EOF`, `<<-EOF` (leading tabs), `<<'EOF'`, `<<"EOF"`, `<<\EOF`.
fn strip_heredoc_bodies(command: &str) -> Option<String> {
    let operator = regex::Regex::new(r#"<<(-?)\s*(?:'([A-Za-z_]\w*)'|"([A-Za-z_]\w*)"|\\?([A-Za-z_]\w*))"#).ok()?;
    let (mut out, mut pending, mut found) = (String::new(), std::collections::VecDeque::new(), false);
    for line in command.split('\n') {
        if let Some((tabs, delimiter)) = pending.front() {
            if (if *tabs { line.trim_start_matches('\t') } else { line }) == delimiter {
                pending.pop_front();
            }
            continue;
        }
        for c in operator.captures_iter(line) {
            let name = c.get(2).or(c.get(3)).or(c.get(4))?.as_str().to_string();
            pending.push_back((&c[1] == "-", name));
            found = true;
        }
        out.push_str(line);
        out.push('\n');
    }
    (found && pending.is_empty()).then_some(out)
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

/// Split commands whose quoting is inert in POSIX shells and zsh, even if not canonical:
/// space/tab-separated words built from `shlex.quote`-safe or alphabetic characters,
/// single-quoted text and double-quoted text without `$`, backquote or `!`, where a backslash
/// may only precede itself or a character it does not escape (`"a\(b"`, regex escapes).
/// Adjacent single-quoted runs (`'a''b'`) are refused because zsh RC_QUOTES reads them
/// differently. Anything else (newlines, globs, `~`, expansions) returns `None`.
fn inert_split(text: &str) -> Option<Vec<String>> {
    let safe = |c: char| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c) || (!c.is_ascii() && c.is_alphabetic());
    let mut chars = text.chars().peekable();
    let mut tokens = Vec::new();
    let mut token: Option<String> = None;
    while let Some(c) = chars.next() {
        match c {
            ' ' | '\t' => tokens.extend(token.take()),
            '\'' => {
                let word = token.get_or_insert_with(String::new);
                loop {
                    match chars.next()? {
                        '\'' => break,
                        c => word.push(c),
                    }
                }
                if chars.peek() == Some(&'\'') {
                    return None;
                }
            }
            '"' => {
                let word = token.get_or_insert_with(String::new);
                loop {
                    match chars.next()? {
                        '"' => break,
                        '$' | '`' | '!' => return None,
                        // Shells and shlex agree on `\\` and on a backslash kept before an ordinary character.
                        '\\' => match chars.next()? {
                            '\\' => word.push('\\'),
                            '$' | '`' | '!' | '"' | '\n' => return None,
                            c => word.extend(['\\', c]),
                        },
                        c => word.push(c),
                    }
                }
            }
            c if safe(c) => token.get_or_insert_with(String::new).push(c),
            _ => return None,
        }
    }
    tokens.extend(token);
    Some(tokens)
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

const AUTO_WRAP_NOTE: &str = "Omega guard ran this command through \u{3a9}-run --shell (bounded result).";

struct Decision {
    kind: &'static str,
    reason: String,
    /// Wrapped form of a denied unwrapped command, when it is safe to run instead.
    rewrite: Option<String>,
    /// Serialized replacement `tool_input` (PreToolUse `updatedInput`).
    updated: Option<String>,
}

impl Decision {
    fn new(kind: &'static str, reason: String) -> Self {
        Decision { kind, reason, rewrite: None, updated: None }
    }

    fn render(decision: Option<&Decision>) -> String {
        let Some(decision) = decision else { return "{}".to_string() };
        let mut out = String::from(r#"{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"#);
        json_string(decision.kind, &mut out);
        out.push_str(r#","permissionDecisionReason":"#);
        json_string(&decision.reason, &mut out);
        if let Some(updated) = &decision.updated {
            out.push_str(r#","updatedInput":"#);
            out.push_str(updated);
            out.push_str(r#","additionalContext":"#);
            json_string(AUTO_WRAP_NOTE, &mut out);
        }
        out.push_str("}}");
        out
    }
}

struct Guard {
    native: String,
    optimize: String,
    guidance: String,
    allow_clean: bool,
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
    /// Match the first executable on PATH, as the shell will, against the trusted binary.
    fn trusted_alias(command: &str, target: &str) -> bool {
        use std::os::unix::fs::PermissionsExt;
        let Some(path) = std::env::var_os("PATH") else { return false };
        for directory in std::env::split_paths(&path) {
            let candidate = directory.join(command);
            let Ok(metadata) = candidate.metadata() else { continue };
            if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
                continue;
            }
            return candidate.canonicalize().ok().zip(std::fs::canonicalize(target).ok())
                .is_some_and(|(actual, expected)| actual == expected);
        }
        false
    }

    fn new(allow_clean: bool) -> Self {
        let native = expanduser("/.agents/tools/robot-exec/native/run");
        let optimize = expanduser("/.agents/tools/optimize");
        let guidance = GUIDANCE_TAIL.replace("{NATIVE}", &native);
        Guard { native, optimize, guidance, allow_clean }
    }

    /// Outcome of a fully checked, policy-clean wrapper call.
    fn clean(&self) -> Option<Decision> {
        self.allow_clean
            .then(|| Decision::new("allow", "Omega guard: wrapped command matched no deny or ask policy.".to_string()))
    }

    fn denied(&self, reason: &str) -> Option<Decision> {
        Some(Decision::new("deny", format!("{reason} {}", self.guidance)))
    }

    /// Deny an unwrapped command, appending the exact wrapped command when one is safe to offer.
    fn denied_unwrapped(&self, reason: &str, command: &str, argv: &[String]) -> Option<Decision> {
        // A miswritten wrapper call is not re-wrapped: nesting would hide the mistake.
        let head = argv.first().map(String::as_str).or_else(|| command.split(py_isspace).next()).unwrap_or_default();
        let head = head.trim_start_matches(['\'', '"']);
        let wrapper = !OMEGA_CLI.contains(&head)
            && (head.contains('\u{3a9}') || head.starts_with("omega") || head.contains("/.agents/tools/"));
        let literal = if wrapper { Self::literal_script(command) } else { None };
        // Retired `Ω-read FILE…` (still called in recorded sessions): bounded `head` of the files
        // instead of a failed round trip.
        let files = argv.get(1..).filter(|files| {
            matches!(head, "\u{3a9}-read" | "omega-read")
                && !files.is_empty()
                && !command.contains(['\n', '#'])
                // `~` and globs would stay literal once quoted: leave them to the deny guidance.
                && files.iter().all(|f| {
                    !f.starts_with(['-', '~']) && !f.contains(['|', '>', '<', ';', '&', '`', '$', '*', '?', '['])
                })
        });
        let retired = files.map(|files| {
            let argv: Vec<String> = ["head", "-n", "200", "--"].iter().map(|s| (*s).to_string()).chain(files.iter().cloned()).collect();
            (shlex_join(&argv), argv)
        });
        let rewrite = match Self::cd_wrapper(command) {
            Some(moved) => Some(moved),
            None if retired.is_some() => retired.as_ref().and_then(|(command, argv)| Self::rewrite(command, argv)),
            None if wrapper => {
                literal.clone().or_else(|| Self::outer_wrapper(command)).or_else(|| self.requote(command)).or_else(|| Self::heredoc_argv(command))
            }
            None => Self::rewrite(command, argv),
        };
        let hint = rewrite.as_ref().filter(|h| h.len() <= MAX_REWRITE_BYTES).map(|h| format!(" Ready-to-run: {h}")).unwrap_or_default();
        // A ready-to-run form already is the fix.
        let inspect = head.ends_with("inspect") || argv.get(1).is_some_and(|a| a == "inspect");
        let correction = if hint.is_empty() { Self::correction(command, wrapper, inspect) } else { "" };
        let mut decision = Decision::new("deny", format!("{reason}{hint}{correction} {}", self.guidance));
        // Unparsed quoting (empty argv) is never run on the model's behalf, except a wrapper script
        // whose outer quotes are unambiguous.
        if !argv.is_empty() || literal.is_some() {
            decision.rewrite = rewrite.filter(|h| h.len() <= MAX_AUTO_WRAP_BYTES);
        }
        Some(decision)
    }

    /// The one fix for a command that is never run on the model's behalf: a heredoc that did not
    /// parse, or a miswritten wrapper call. Empty when the generic guidance is all there is to say.
    fn correction(command: &str, wrapper: bool, inspect: bool) -> &'static str {
        // Quote-aware: an operator inside an argument is not an outer one.
        let (mut quote, mut outer, mut live) = (None::<char>, false, false);
        let mut chars = command.chars();
        while let Some(c) = chars.next() {
            match (quote, c) {
                (None, '\'' | '"') => quote = Some(c),
                (None, '\\') => _ = chars.next(),
                (None, '|' | ';' | '&' | '<' | '>' | '\n') => outer = true,
                (Some(q), _) if c == q => quote = None,
                (Some('"'), '\\') => live |= matches!(chars.next(), Some('$' | '`' | '!' | '"' | '\n')),
                (Some('"'), '$' | '`' | '!') => live = true,
                _ => {}
            }
        }
        // `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`, `<<\EOF`; not a quoted "<<" or a spaced shift.
        let heredoc = command.match_indices("<<").any(|(at, _)| {
            let rest = command[at + 2..].trim_start_matches('-');
            let rest = rest.strip_prefix(['\'', '"', '\\']).unwrap_or(rest);
            rest.starts_with(|c: char| c.is_ascii_alphabetic() || c == '_')
        });
        if heredoc {
            " Heredoc: create the file with the file-writing tool, then run it as argv."
        } else if !wrapper {
            ""
        } else if outer && inspect {
            " \u{3a9}-inspect is already bounded: drop the outer pipe, redirection or chaining and select with --search, --offset, --lines, --max-bytes; one call per slice."
        } else if outer {
            " Nothing may precede or follow the wrapper: drop the outer pipe, redirection or chaining; one wrapper per call, independent calls in parallel."
        } else if live {
            " Double quotes keep $, backquote and ! live: single-quote that argument, or write the script to a file and run it as argv."
        } else {
            ""
        }
    }

    /// Claude only: run the wrapped form of an unwrapped command instead of denying it, when that
    /// exact form would be allowed had the model written it. Saves one full-context round trip.
    fn auto_wrap(&self, decision: Option<Decision>, raw: &[u8]) -> Option<Decision> {
        let wrapped = decision.as_ref().and_then(|d| d.rewrite.clone()).filter(|_| self.allow_clean);
        let updated = wrapped.and_then(|wrapped| {
            let mut payload = serde_json::from_slice::<serde_json::Value>(raw).ok()?;
            if payload["tool_name"] != "Bash" || !payload["tool_input"]["command"].is_string() {
                return None;
            }
            if !self.check(&wrapped).is_some_and(|d| d.kind == "allow") {
                return None;
            }
            payload["tool_input"]["command"] = serde_json::Value::String(wrapped);
            Some(payload["tool_input"].to_string())
        });
        match updated {
            Some(updated) => {
                let mut allow =
                    Decision::new("allow", "Omega guard: unwrapped command auto-wrapped; it matched no deny or ask policy.".to_string());
                allow.updated = Some(updated);
                Some(allow)
            }
            None => decision,
        }
    }

    /// `cat`/`head`/`tail` of evidence become a bounded `Ω-inspect`; any other command is
    /// wrapped in `Ω-run --shell` unless the policy would still deny or ask for it.
    fn rewrite(command: &str, argv: &[String]) -> Option<String> {
        const EVIDENCE: &str = ".agents/tasks/evidence/";
        // Chaining, redirection or a consumer other than head/tail/sed would be dropped by the
        // rewrite: leave it to the generic wrap.
        let plain = !command.contains('\n')
            && argv.iter().all(|a| matches!(a.as_str(), "|" | "2>&1" | "2>/dev/null") || !a.contains([';', '&', '>', '|']))
            && argv.split(|a| a == "|").skip(1).all(|s| matches!(s.first().map(String::as_str), Some("head" | "tail" | "sed")));
        // Exactly one path operand, so no other file is dropped; `~/` and `$HOME/` are expanded
        // because the rewrite quotes the path.
        let mut paths = argv.iter().skip(1).filter(|a| a.contains('/') && !a.starts_with("2>"));
        let path = paths.next().filter(|a| a.contains(EVIDENCE) && paths.next().is_none());
        let reader = plain && matches!(argv.first().map(String::as_str), Some("cat" | "head" | "tail"));
        if let Some(path) = path.filter(|_| reader) {
            let path = path.strip_prefix("~/").or_else(|| path.strip_prefix("$HOME/")).map_or(path.clone(), |rest| expanduser(&format!("/{rest}")));
            let path = path.trim_end_matches('/');
            // A glob file name (`stdout*`) names a file of the directory, never the directory.
            let glob = |name: &str| name.contains(['*', '?', '[']);
            let (dir, stream) = match path.rsplit_once('/') {
                Some((dir, "stdout.log" | "stdout")) => (dir, "stdout"),
                Some((dir, "stderr.log" | "stderr")) => (dir, "stderr"),
                Some((dir, "result.json")) => (dir, "auto"),
                Some((dir, name)) if glob(name) && name.starts_with("stdout") => (dir, "stdout"),
                Some((dir, name)) if glob(name) && name.starts_with("stderr") => (dir, "stderr"),
                Some((dir, name)) if glob(name) => (dir, "auto"),
                _ => (path, "auto"),
            };
            // A directory that only the shell can resolve stays with the generic wrap.
            if !dir.contains(['~', '$', '*', '?', '[']) {
                let args = ["\u{3a9}-inspect", "--stream", stream, "--lines", "40", "--max-bytes", "7000", dir];
                return Some(shlex_join(&args.map(String::from)));
            }
        }
        if policy(command).is_some() {
            return None;
        }
        Some(shlex_join(&["\u{3a9}-run".to_string(), "--shell".to_string(), command.to_string()]))
    }

    /// `'Ω-run' [OPTIONS] --shell 'SCRIPT'` whose SCRIPT holds apostrophes (`s.replace('a', 'b')` in
    /// a heredoc): the shell would cut it into pieces, but the text between the first quote after
    /// `--shell` and the final quote is the script meant. Returns it canonically quoted. Recorded:
    /// 107 such calls denied, each followed by a rewrite into a file.
    fn literal_script(command: &str) -> Option<String> {
        let call = regex::Regex::new(
            r"(?s)^((?:'\x{3a9}-run'|\x{3a9}-run|omega-run|\S*/\.agents/tools/robot-exec/native/run\s+run)(?:\s+[^\s']+)*?)\s+--shell\s+'(.*)'((?:\s+--(?:cwd|timeout|inline-bytes)(?:\s+|=)[^\s']+)*)$",
        )
        .ok()?;
        let parts = call.captures(command)?;
        let script = &parts[2];
        // Options written after the script (`--shell '…' --cwd DIR`) move before it.
        let mut argv = inert_split(&format!("{} {}", &parts[1], &parts[3]))?;
        // `--shell-executable PATH` is an option; a second `--shell` is not.
        if !script.contains('\'') || argv.iter().any(|a| a == "--shell" || a.starts_with("--shell=")) {
            return None;
        }
        // Escaped apostrophes (`'"'"'`, `'\''`) mean the quoting is deliberate: a raw one is then
        // a slip only inside a heredoc body (`<<'"'"'EOF'"'"'` then `episode's`), which is data.
        if script.contains("'\"'\"'") || script.contains("'\\''") {
            let opener = regex::Regex::new(r#"<<-?[ \t]*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?"#).ok()?;
            let (mut raw, mut body_end, mut decoded) = (false, None::<String>, Vec::new());
            for line in script.split('\n') {
                let plain = line.replace("'\"'\"'", "'").replace("'\\''", "'");
                let slip = line.replace("'\"'\"'", "").replace("'\\''", "").contains('\'');
                match &body_end {
                    Some(end) if plain.trim_start_matches('\t') == end => body_end = None,
                    Some(_) => raw |= slip,
                    None if slip => return None,
                    None => body_end = opener.captures(&plain).map(|c| c[1].to_string()),
                }
                decoded.push(plain);
            }
            // Without a slip the quoting already is what was meant.
            if body_end.is_some() || !raw {
                return None;
            }
            let script = decoded.join("\n");
            if ["\u{3a9}-run", "omega-run", "/.agents/tools/"].iter().any(|w| script.contains(w)) {
                return None;
            }
            argv.extend(["--shell".to_string(), script]);
            return Some(shlex_join(&argv));
        }
        // Text the shell reads unquoted holds words of the script (`a`, `, `, `b`), not only the
        // space between separate arguments (`'a' 'b'`); a second wrapper call means a chain.
        let outside: String = script.split('\'').skip(1).step_by(2).collect();
        let chained = ["\u{3a9}-run", "omega-run", "/.agents/tools/"].iter().any(|w| script.contains(w));
        if !outside.chars().any(|c| !c.is_whitespace()) || chained {
            return None;
        }
        argv.extend(["--shell".to_string(), script.to_string()]);
        Some(shlex_join(&argv))
    }

    /// A wrapper call written as argv whose quoting the shell would not take as written, but whose
    /// meaning is unambiguous: `!` or `\"` inside double quotes (`rg "(?!x)"`), `(`, `)`, `^` or a
    /// backslash escape outside quotes (`rg -F -e self.reset()`). Returns `shlex.join(argv)`. Not
    /// when a shell would expand or join something shlex keeps (`$`, backquote, `~`, `{`, `#`,
    /// `\$`, backslash-newline, a glob except as a `-g`/`--glob`/`--include` pattern) or an
    /// operator splits the command. Recorded: 39 such calls denied without a ready-to-run form.
    fn requote(&self, command: &str) -> Option<String> {
        // Split the trimmed text itself: the caller's argv may keep edge characters it dropped.
        let argv = shlex_split(command)?;
        let head = argv.first()?.as_str();
        let wrapper = matches!(head, "\u{3a9}-run" | "omega-run" | "\u{3a9}-inspect" | "omega-inspect") || head == self.native;
        if !wrapper {
            return None;
        }
        // A glob after a glob option is the option's pattern (`rg -g !*.md`), never meant for the shell.
        let glob_value = |word_start: usize| {
            let word = &command[word_start..];
            let flag = command[..word_start].trim_end_matches([' ', '\t']).rsplit([' ', '\t']).next().unwrap_or_default();
            matches!(flag, "-g" | "--glob" | "--iglob" | "--include" | "--exclude")
                || ["--glob=", "--iglob=", "--include=", "--exclude="].iter().any(|p| word.starts_with(p))
        };
        let (mut quote, mut word_start, mut chars) = (None::<char>, 0, command.char_indices().peekable());
        while let Some((at, c)) = chars.next() {
            match (quote, c) {
                (None, ' ' | '\t') => word_start = at + 1,
                (None, '\'' | '"') => quote = Some(c),
                (None, '\\') => {
                    if matches!(chars.next(), None | Some((_, '\n'))) {
                        return None;
                    }
                }
                (None, c) if c.is_alphanumeric() || "_@%+=:,./-()^!".contains(c) => {}
                (None, '*' | '?' | '[' | ']') if glob_value(word_start) => {}
                (None, _) => return None,
                (Some('\''), '\'') => {
                    quote = None;
                    // zsh RC_QUOTES reads `''` inside single quotes as an apostrophe.
                    if chars.peek().map(|&(_, c)| c) == Some('\'') {
                        return None;
                    }
                }
                (Some('"'), '"') => quote = None,
                (Some('"'), '$' | '`') => return None,
                (Some('"'), '\\') => {
                    if matches!(chars.next(), None | Some((_, '$' | '`' | '\n'))) {
                        return None;
                    }
                }
                _ => {}
            }
        }
        let joined = shlex_join(&argv);
        (quote.is_none() && joined != command).then_some(joined)
    }

    /// `'Ω-run' [OPTIONS] -- python3 - <<'EOF'` + body + `EOF`: the heredoc feeds the outer shell's
    /// wrapper call, which passes no stdin on. Moved into `--shell` with the command, it feeds the
    /// command meant. Recorded: 9 such calls denied, each rewritten into a file.
    fn heredoc_argv(command: &str) -> Option<String> {
        let at = command.find("<<")?;
        let mut argv = inert_split(command[..at].trim_end_matches([' ', '\t']))?;
        let dashes = argv.iter().position(|a| a == "--")?;
        if !matches!(argv[0].as_str(), "\u{3a9}-run" | "omega-run") || dashes + 1 == argv.len() || argv.iter().any(|a| a == "--shell") {
            return None;
        }
        let heredoc = &command[at..];
        let op = regex::Regex::new(r#"^<<-?[ \t]*(['"]?)([A-Za-z_][A-Za-z0-9_]*)(['"]?)[ \t]*\n"#).ok()?;
        let parts = op.captures(heredoc)?;
        let body = &heredoc[parts[0].len()..];
        // Exactly one body, closed by its delimiter on the last line: nothing runs after it.
        let body = body.trim_end_matches(py_isspace);
        let (inner, last) = body.rsplit_once('\n').unwrap_or(("", body));
        let early = inner.lines().any(|line| line.trim_start_matches('\t') == &parts[2]);
        if parts[1] != parts[3] || last.trim_start_matches('\t') != &parts[2] || early {
            return None;
        }
        let script = format!("{} {}", shlex_join(&argv[dashes + 1..]), heredoc.trim_end_matches(py_isspace));
        argv.truncate(dashes);
        argv.extend(["--shell".to_string(), script]);
        Some(shlex_join(&argv))
    }

    /// A wrapper call with something outside it: `CALL > FILE`, `CALL 2>&1 | head -5`, `CALL; true`,
    /// `CALL; sed -n 1,80p f`. The outer redirection or filter moves into the script, a trailing
    /// `true` is dropped and a plain command after the call gets its own wrapper call. Recorded:
    /// about 30 such calls denied, each resent by hand. None when the first command is not a
    /// wrapper call or a plain command could change the state later calls rely on (`cd`, `X=`).
    fn outer_wrapper(command: &str) -> Option<String> {
        let run = |head: &str| matches!(head, "\u{3a9}-run" | "omega-run");
        // Split at unquoted `;`, `&&`, `||` and newlines, keeping each separator.
        let (mut quote, mut parts, mut start) = (None::<char>, Vec::new(), 0);
        let mut chars = command.char_indices().peekable();
        while let Some((at, c)) = chars.next() {
            let next = chars.peek().map(|&(_, c)| c);
            match (quote, c) {
                (None, '\'' | '"') => quote = Some(c),
                (None, '\\' | '`' | '$' | '(' | ')' | '<') => return None,
                (None, ';' | '\n') => {
                    parts.push((&command[start..at], &command[at..at + 1]));
                    start = at + 1;
                }
                (None, '&' | '|') if next == Some(c) => {
                    chars.next();
                    parts.push((&command[start..at], &command[at..at + 2]));
                    start = at + 2;
                }
                (None, '&') if !matches!(command[..at].chars().last(), Some('>')) && next != Some('>') => return None,
                (Some(q), _) if c == q => quote = None,
                _ => {}
            }
        }
        if quote.is_some() {
            return None;
        }
        parts.push((&command[start..], ""));
        let mut out = String::new();
        let mut changed = false;
        let last = parts.len() - 1;
        for (index, (segment, separator)) in parts.into_iter().enumerate() {
            let segment = segment.trim_matches(py_isspace);
            let head = segment.split(py_isspace).next().unwrap_or_default().trim_matches(['\'', '"']);
            let call = if matches!(segment, "true" | "echo") && index == last && index > 0 {
                // `CALL; true`, `CALL || true`, `CALL; echo`: the wrapper already reports the exit status.
                changed = true;
                out.truncate(out.trim_end_matches([';', '&', '|', ' ', '\n']).len());
                break;
            } else if matches!(head, "\u{3a9}-inspect" | "omega-inspect") {
                // Checked alone by the chain rule (`Ω-inspect … | head` included).
                segment.to_string()
            } else if run(head) && index == 0 && index != last && {
                // `'Ω-run' --shell-keep-going 2>/dev/null; CALL`: options and no command, a call that
                // only fails (recorded 8 times as if it set a mode for the next call).
                let bare = segment.split(['>', '|']).next().unwrap_or_default().trim_end().trim_end_matches(['1', '2']);
                inert_split(bare).is_some_and(|a| a[1..].iter().all(|o| o.starts_with("--") && o != "--shell" && !o.starts_with("--shell=") && o != "--"))
            } {
                changed = true;
                continue;
            } else if run(head) {
                // First unquoted `|` or `>` (with its fd digit) ends the call.
                let (mut quote, mut cut) = (None::<char>, None);
                for (at, c) in segment.char_indices() {
                    match (quote, c) {
                        (None, '\'' | '"') => quote = Some(c),
                        (None, '|' | '>') => {
                            let fd = segment[..at].ends_with(['1', '2'])
                                && segment[..at - 1].ends_with(py_isspace);
                            cut = Some(if fd { at - 1 } else { at });
                            break;
                        }
                        (Some(q), _) if c == q => quote = None,
                        _ => {}
                    }
                }
                match cut {
                    None => segment.to_string(),
                    Some(cut) => {
                        let tail = segment[cut..].trim_matches(py_isspace);
                        let mut argv = inert_split(&segment[..cut])?;
                        let script = match argv.iter().position(|a| a == "--shell" || a == "--") {
                            Some(at) if argv[at] == "--shell" && at + 2 == argv.len() => argv.pop()?,
                            Some(at) if argv[at] == "--" && at + 1 < argv.len() => {
                                let script = shlex_join(&argv[at + 1..]);
                                argv.truncate(at);
                                argv.push("--shell".to_string());
                                script
                            }
                            _ => return None,
                        };
                        let script = if script.contains([';', '&', '|', '\n']) {
                            format!("{{ {script}\n}} {tail}")
                        } else {
                            format!("{script} {tail}")
                        };
                        argv.push(script);
                        changed = true;
                        shlex_join(&argv)
                    }
                }
            } else {
                // A state change would not reach later calls once wrapped apart.
                let word = segment.split(py_isspace).next().unwrap_or_default();
                let stateful = matches!(word, "cd" | "pushd" | "popd" | "export" | "unset" | "source" | "." | "set" | "alias")
                    || word.contains('=');
                if index == 0 || segment.is_empty() || (stateful && index != last) || segment.contains("/.agents/tools/") {
                    return None;
                }
                changed = true;
                shlex_join(&["\u{3a9}-run".to_string(), "--shell".to_string(), segment.to_string()])
            };
            out.push_str(&call);
            if !separator.is_empty() {
                out.push_str(if separator == "\n" { ";" } else { separator });
                out.push(' ');
            }
        }
        (changed && !out.is_empty()).then(|| out.trim_end_matches(py_isspace).to_string())
    }

    /// `cd DIR && 'Ω-run' ARGS` written with inert quoting becomes `'Ω-run' --cwd DIR ARGS`:
    /// wrapping it whole would nest one result JSON, escaped, inside another.
    fn cd_wrapper(command: &str) -> Option<String> {
        let (left, right) = command.split_once("&&")?;
        let [cd, dir] = &inert_split(left)?[..] else { return None };
        let mut rest = inert_split(right)?;
        let run = matches!(rest.first().map(String::as_str), Some("\u{3a9}-run" | "omega-run"));
        if cd != "cd" || !run || rest.iter().any(|a| a == "--cwd" || a.starts_with("--cwd=")) {
            return None;
        }
        rest.splice(1..1, ["--cwd".to_string(), dir.clone()]);
        Some(shlex_join(&rest))
    }

    /// `CALL; CALL && CALL || CALL` where every call is a wrapper call: the calls, split at the unquoted
    /// `;`, `&&` and `||`. Each is then checked as if written alone.
    fn wrapper_chain(command: &str) -> Option<Vec<String>> {
        let (mut quote, mut calls, mut start) = (None::<char>, Vec::new(), 0);
        let mut chars = command.char_indices().peekable();
        while let Some((at, c)) = chars.next() {
            match (quote, c) {
                (None, '\'' | '"') => quote = Some(c),
                (None, '\\') => _ = chars.next(),
                (None, ';') => {
                    calls.push(&command[start..at]);
                    start = at + 1;
                }
                (None, '&' | '|') if chars.peek().map(|&(_, next)| next) == Some(c) => {
                    chars.next();
                    calls.push(&command[start..at]);
                    start = at + 2;
                }
                (Some(q), _) if c == q => quote = None,
                _ => {}
            }
        }
        calls.push(&command[start..]);
        let wrapper = |call: &str| {
            let head = call.split(py_isspace).next().unwrap_or_default().trim_matches(['\'', '"']);
            matches!(head, "\u{3a9}-run" | "\u{3a9}-inspect" | "omega-run" | "omega-inspect") || head.contains("/.agents/tools/")
        };
        let calls: Vec<String> = calls.iter().map(|c| c.trim_matches(py_isspace).to_string()).collect();
        (quote.is_none() && calls.len() > 1 && calls.iter().all(|c| wrapper(c))).then_some(calls)
    }

    /// The command the wrapper will execute, as text for policy matching.
    fn inner_text(argv: &[String]) -> String {
        if argv[1] != "run" {
            return String::new();
        }
        // Every text the executor may run (it stops option parsing at `--` or the first word and
        // accepts `--shell=V`): deny beats ask across the `\n`-joined texts.
        let mut texts: Vec<String> = Vec::new();
        for (index, arg) in argv.iter().enumerate().skip(2) {
            if arg == "--shell" {
                texts.extend(argv.get(index + 1).cloned());
            } else if let Some(script) = arg.strip_prefix("--shell=") {
                texts.push(script.to_string());
            }
        }
        texts.push(shlex_join(&argv[2..]));
        // Command start as the executor finds it: the first word that is neither an option nor its value.
        let mut start = if argv.get(2).is_some_and(|a| a == "--native-only") { 3 } else { 2 };
        while let Some(arg) = argv.get(start).filter(|a| a.len() > 1 && a.starts_with('-') && *a != "--") {
            let flag = arg.contains('=')
                || (arg != "--shell" && ["--validation", "--fail-fast", "--shell-keep-going"].iter().any(|f| f.starts_with(arg.as_str())));
            start += if flag { 1 } else { 2 };
        }
        if argv.get(start).is_some_and(|a| a == "--") {
            start += 1;
        }
        texts.push(shlex_join(argv.get(start..).unwrap_or_default()));
        if let Some(index) = argv.iter().position(|a| a == "--") {
            texts.push(shlex_join(&argv[index + 1..]));
        }
        texts.join("\n")
    }

    /// A filter stage that names no file: a file operand, `-f FILE` or a recursive search would read
    /// outside the bounded reader. Options whose separate value is not listed count that value as an
    /// operand, which refuses the stage (safe side).
    fn reads_only_stdin(stage: &[String]) -> bool {
        let search = matches!(stage[0].as_str(), "grep" | "rg");
        let values: &[&str] = match stage[0].as_str() {
            "head" | "tail" => &["-n", "-c"],
            "cut" => &["-b", "-c", "-d", "-f"],
            "grep" | "rg" => &["-e", "-m", "-A", "-B", "-C"],
            _ => &[],
        };
        let (mut operands, mut pattern, mut ended) = (0, false, false);
        let mut args = stage[1..].iter();
        while let Some(arg) = args.next() {
            if ended || !arg.starts_with('-') {
                operands += 1;
            } else if arg == "--" {
                ended = true;
            } else if search
                && (["--file", "--recursive", "--dereference"].iter().any(|o| arg.starts_with(o))
                    || (!arg.starts_with("--") && arg.contains(['f', 'r', 'R'])))
            {
                return false;
            } else if values.contains(&arg.as_str()) {
                pattern |= arg == "-e";
                args.next();
            } else {
                pattern |= search && (arg.starts_with("-e") || arg.starts_with("--regexp"));
            }
        }
        operands <= usize::from(search && !pattern)
    }

    /// `Ω-inspect|Ω-resume|Ω-optimize ARGS [2>&1] | head|tail|grep|rg|wc|cut ARGS` written in canonical
    /// quoting: returns the reader call alone. Anything else (other consumers, chains, redirections) stays denied.
    fn inspect_pipe(command: &str, argv: &[String]) -> Option<String> {
        let stages: Vec<&[String]> = argv.split(|a| a == "|").collect();
        let (left, filters) = stages.split_first()?;
        let (left, redirect) = match left.split_last() {
            Some((last, rest)) if last == "2>&1" || last == "2>/dev/null" => (rest, last.as_str()),
            _ => (*left, ""),
        };
        // Bounded readers: `Ω-inspect`, and the fixed `Ω-resume` / `Ω-optimize` reports.
        let inspect = matches!(
            left.first().map(String::as_str),
            Some("\u{3a9}-inspect" | "omega-inspect" | "\u{3a9}-resume" | "omega-resume" | "\u{3a9}-optimize" | "omega-optimize")
        );
        // Read-only filters; `cut -c1-200` is the most common recorded one.
        let filter = |stage: &&[String]| {
            matches!(stage.first().map(String::as_str), Some("head" | "tail" | "grep" | "rg" | "wc" | "cut"))
                && !stage.iter().any(|a| a.starts_with("--pre") || a.starts_with("--hostname-bin"))
                && Self::reads_only_stdin(stage)
                && policy(&shlex_join(stage)).is_none()
        };
        if !inspect || filters.is_empty() || !filters.iter().all(filter) {
            return None;
        }
        let joined = shlex_join(left);
        let mut canonical = joined.clone();
        if !redirect.is_empty() {
            canonical = format!("{canonical} {redirect}");
        }
        for stage in filters {
            canonical = format!("{canonical} | {}", shlex_join(stage));
        }
        if command == canonical {
            return Some(joined);
        }
        // Inert quoting the model writes by habit (`--search 'error'`): each stage between the
        // unquoted pipes must split, with nothing live, into exactly the words parsed above.
        let (mut quote, mut bars) = (None::<char>, vec![0]);
        for (at, c) in command.char_indices() {
            match (quote, c) {
                (None, '\'' | '"') => quote = Some(c),
                (None, '|') => bars.push(at + 1),
                (Some(q), _) if c == q => quote = None,
                _ => {}
            }
        }
        if bars.len() != stages.len() {
            return None;
        }
        bars.push(command.len() + 1);
        let texts: Vec<&str> = bars.windows(2).map(|w| command[w[0]..w[1] - 1].trim_matches(' ')).collect();
        let head = if redirect.is_empty() { texts[0] } else { texts[0].strip_suffix(redirect)?.trim_end_matches(' ') };
        let same = |text: &str, words: &[String]| inert_split(text).is_some_and(|split| split == words);
        (same(head, left) && texts[1..].iter().zip(filters).all(|(text, words)| same(text, words))).then_some(joined)
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
        match command {
            Some(Json::Str(command)) if !command.trim_matches(py_isspace).is_empty() => self.check(command),
            _ => self.denied("Missing shell command."),
        }
    }

    /// The decision for one shell command string.
    fn check(&self, command: &str) -> Option<Decision> {
        let Some(mut argv) = shlex_split(command) else {
            let command = command.trim_matches(py_isspace);
            // A heredoc body is data to the shell (`dataset's` in a Python script): only the lines
            // around it must parse. The policy still reads the whole text, body included.
            return match strip_heredoc_bodies(command).as_deref().and_then(shlex_split).filter(|a| !a.is_empty()) {
                Some(outside) => self.denied_unwrapped("Unwrapped or noncanonical shell command.", command, &outside),
                None => self.denied_unwrapped("Malformed shell quoting.", command, &[]),
            };
        };
        let command = command.trim_matches(py_isspace);
        if Self::literal_script(command).is_some() {
            // Its apostrophes would silently drop quotes or cut the script: run it as written.
            return self.denied_unwrapped("Unwrapped or noncanonical shell command.", command, &argv);
        }
        if let Some(inspect) = Self::inspect_pipe(command, &argv) {
            // The reader is checked like the bare call; the filters were policy-checked in `inspect_pipe`.
            return self.check(&inspect);
        }
        if let Some(calls) = Self::wrapper_chain(command) {
            // Batched wrapper calls: each is checked alone and keeps its own bounded result.
            let clean = self.clean().map(|d| d.kind);
            let all_clean = calls.into_iter().all(|call| {
                self.check(&call).map(|d| d.kind) == clean
            });
            if all_clean {
                return self.clean();
            }
        }
        if argv.is_empty() || (command != shlex_join(&argv) && inert_split(command).as_ref() != Some(&argv)) {
            return self.denied_unwrapped("Unwrapped or noncanonical shell command.", command, &argv);
        }
        if OMEGA_CLI.contains(&argv[0].as_str()) {
            if !Self::trusted_alias(&argv[0], &self.optimize) {
                return self.denied("Untrusted omega command on PATH.");
            }
            argv[0] = self.optimize.clone();
        }
        if argv[0] == self.optimize {
            // Fixed native CLI, no shell evaluation.
            return self.clean();
        }
        let alias = match argv[0].as_str() {
            "omega-run" | "Ω-run" => Some("run"),
            "omega-inspect" | "Ω-inspect" => Some("inspect"),
            "omega-prune" | "Ω-prune" => Some("prune"),
            _ => None,
        };
        if let Some(subcommand) = alias {
            if !Self::trusted_alias(&argv[0], &self.native) {
                return self.denied("Untrusted omega command on PATH.");
            }
            argv[0] = self.native.clone();
            argv.insert(1, subcommand.to_string());
        }
        // Canonical quoting makes metacharacters literal arguments to the wrapper.
        // Without --allow-clean, native sandbox and approval checks still apply.
        if argv.len() < 2 || argv[0] != self.native || !matches!(argv[1].as_str(), "run" | "inspect" | "prune") {
            return self.denied_unwrapped("Unwrapped or noncanonical shell command.", command, &argv);
        }
        // zsh EQUALS expansion rewrites an unquoted "=cmd" into a path; a quoted one stays text.
        let unquoted_equals = {
            let (mut quote, mut start, mut found) = (None::<char>, true, false);
            for c in command.chars() {
                match (quote, c) {
                    (None, '=') if start => found = true,
                    (None, '\'' | '"') => quote = Some(c),
                    (Some(q), _) if c == q => quote = None,
                    _ => {}
                }
                start = quote.is_none() && matches!(c, ' ' | '\t');
            }
            found
        };
        if unquoted_equals && argv.iter().any(|arg| arg.starts_with('=')) {
            return self.denied("Argument starting with \"=\" is expanded by zsh.");
        }
        if argv[1] == "prune" && argv.iter().any(|a| {
            let name = a.split('=').next().unwrap_or_default();
            name.len() > 2 && "--apply".starts_with(name)
        }) {
            return Some(Decision::new("ask", "Deleting old evidence requires explicit user approval.".to_string()));
        }
        policy(&Self::inner_text(&argv)).or_else(|| self.clean())
    }
}

fn run() -> String {
    let guard = Guard::new(std::env::args().nth(1).as_deref() == Some("--allow-clean"));
    let mut input = Vec::new();
    if std::io::stdin().read_to_end(&mut input).is_err() {
        return Decision::render(guard.denied("Guard internal error (OSError).").as_ref());
    }
    let parsed = std::str::from_utf8(&input).map_err(|_| Fail::Invalid).and_then(parse_json);
    let decision = match parsed {
        Ok(payload) => guard.auto_wrap(guard.evaluate(&payload), &input),
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
