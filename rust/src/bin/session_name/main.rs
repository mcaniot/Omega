//! UserPromptSubmit + SessionStart hook: titles the session "Prénom — branche" and tells the agent its name.
//!
//! Session naming hook (see `hooks/test_session_name.py` for the output and registry contract).
//! Any error prints nothing: naming must never block a prompt.
//!
//! Test-only overrides: SESSION_NAMES_REGISTRY (registry path) and
//! SESSION_NAMES_NOW (seconds since epoch, float).

mod branch;
mod json;

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};

use json::J;

const ACTIVE_FOR: f64 = 24.0 * 3600.0; // a session unseen for a day frees its name
const SEP: &str = " — ";

const NAMES: &str = "\
Abel Ada Aiko Alba Alix Alma Amos Ana Anouk Arlo Aster Aube Ava Axel \
Bao Basile Bea Bela Benji Blaise Bo Bran Brune Cael Cali Cami Cass Cleo \
Cosme Dana Dario Dina Dora Eda Eden Eli Elio Elsa Emil Ena Enzo Eva Ezra \
Fana Faro Fido Flo Gaby Gael Gia Gil Gina Hana Hugo Ida Ilan Ilia Ilo \
Ines Ira Iris Isa Ivo Jade Jani Jem Jo Joa Juno Kai Kali Kaya Kenzo Kira \
Koa Lana Lea Leni Leo Lia Lila Lino Liv Lola Lou Luca Lumi Luna Lya Mael \
Mali Mara Marlo Mattis Maya Mila Milo Mina Mira Mita Moana Nael Nala Nao \
Nate Neo Nia Nico Nils Nina Noa Noe Nola Nora Oda Odin Olga Oli Omer Ona \
Oren Otto Pablo Paco Pia Pim Pola Remi Rena Rio Risa Romy Rosa Ruben Ruth \
Sacha Sami Sana Sara Sela Siko Simi Sora Suna Taio Talia Tami Tara Tavi \
Teo Tia Timo Tino Toma Toni Uma Uri Vada Valo Vera Vito Wanda Wren Yael \
Yann Yara Yoko Yuki Yuna Yves Zack Zana Zara Zelie Zeno Zia Ziggy Zoe \
Zola Zuri Aria Bento Ciro Dahlia Edda Fleur Gwen Hiro Ilse Jules Kenji \
Ari Bruno Celia Dimi Elan Faye Gus Hanae Ivy";

/// Stands for any Python exception: the hook prints nothing.
pub struct Abort;

/// Python `str.strip()`: Unicode whitespace including U+001C..U+001F.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

fn registry_path() -> Result<PathBuf, Abort> {
    if let Some(p) = std::env::var_os("SESSION_NAMES_REGISTRY") {
        return Ok(PathBuf::from(p));
    }
    let home = std::env::var_os("HOME").ok_or(Abort)?;
    Ok(PathBuf::from(home).join(".claude").join("session-names.json"))
}

fn now() -> f64 {
    if let Some(v) = std::env::var_os("SESSION_NAMES_NOW") {
        if let Some(t) = v.to_str().and_then(|s| s.parse::<f64>().ok()) {
            return t;
        }
    }
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

fn read_registry(path: &PathBuf) -> Result<Vec<(String, J)>, Abort> {
    let parsed = fs::read(path)
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .and_then(|s| serde_json::from_str::<J>(&s).ok());
    match parsed {
        None => Ok(Vec::new()),
        Some(J::Obj(entries)) => Ok(entries),
        Some(_) => Err(Abort), // Python: .items() on a non-dict
    }
}

fn hashed_start(session: &str, count: usize) -> usize {
    let digest = Sha256::digest(session.as_bytes());
    let n = count as u64;
    digest.iter().fold(0u64, |r, b| (r * 256 + u64::from(*b)) % n) as usize
}

/// (name, name last told to the agent): kept from the title or registry, else hashed and probed past live names.
/// With `assign` false an unknown session gets (None, None) and nothing is written.
fn claim(session: &str, current: &str, now: f64, assign: bool) -> Result<(Option<String>, Option<J>), Abort> {
    let path = registry_path()?;
    let lock = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(path.with_extension("lock"))
        .map_err(|_| Abort)?;
    loop {
        // SAFETY: the descriptor is owned by `lock`, which outlives the call.
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } == 0 {
            break;
        }
        if std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
            return Err(Abort);
        }
    }

    let mut reg = Vec::new();
    for (s, v) in read_registry(&path)? {
        let seen = match (&v, v.get("seen")) {
            (J::Obj(_), None) => 0.0,
            (J::Obj(_), Some(J::Int(i))) => *i as f64,
            (J::Obj(_), Some(J::Float(f))) => *f,
            (J::Obj(_), Some(J::Bool(b))) => f64::from(u8::from(*b)),
            _ => return Err(Abort),
        };
        if now - seen < ACTIVE_FOR {
            reg.push((s, v));
        }
    }
    let known = reg.iter().find(|(s, _)| s == session).map(|(_, v)| v);
    let told = known.and_then(|v| v.get("told")).filter(|t| **t != J::Null).cloned();
    let registered = match known.and_then(|v| v.get("name")) {
        Some(J::Str(s)) if !s.is_empty() => Some(s.clone()),
        Some(v) if v.truthy() => return Err(Abort),
        _ => None,
    };
    let name = if current.is_empty() { registered } else { Some(current.to_owned()) };
    let name = match name {
        Some(n) => n,
        None if !assign => return Ok((None, None)),
        None => {
            let mut taken = Vec::new();
            for (_, v) in reg.iter().filter(|(s, _)| s != session) {
                match v.get("name") {
                    Some(J::Str(n)) => taken.push(n.as_str()),
                    Some(J::Arr(_) | J::Obj(_)) | None => return Err(Abort),
                    Some(_) => {}
                }
            }
            let names: Vec<&str> = NAMES.split_whitespace().collect();
            let start = hashed_start(session, names.len());
            // Falls back to the hashed name if all are taken.
            (0..names.len())
                .map(|i| names[(start + i) % names.len()])
                .find(|n| !taken.contains(n))
                .unwrap_or(names[start])
                .to_owned()
        }
    };

    let entry = J::Obj(vec![
        ("name".into(), J::str(&name)),
        ("seen".into(), J::Float(now)),
        ("told".into(), J::str(&name)),
    ]);
    match reg.iter_mut().find(|(s, _)| s == session) {
        Some(slot) => slot.1 = entry,
        None => reg.push((session.to_owned(), entry)),
    }
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, J::Obj(reg).dumps(true)).map_err(|_| Abort)?;
    fs::rename(&tmp, &path).map_err(|_| Abort)?;
    Ok((Some(name), told))
}

/// Optional string field with Python's `event.get(key) or default`; other truthy types abort.
fn text_field(event: &J, key: &str, default: &str) -> Result<String, Abort> {
    match event.get(key) {
        Some(v) if v.truthy() => match v {
            J::Str(s) => Ok(s.clone()),
            _ => Err(Abort),
        },
        _ => Ok(default.to_owned()),
    }
}

/// Hook output: the title on UserPromptSubmit when it changes; the name in context whenever the agent may not know it.
fn answer(event: &J) -> Result<Option<J>, Abort> {
    if !matches!(event, J::Obj(_)) {
        return Err(Abort);
    }
    let session = text_field(event, "session_id", "")?;
    if session.is_empty() {
        return Ok(None);
    }
    let hook = match event.get("hook_event_name") {
        Some(v) if v.truthy() => v.clone(),
        _ => J::str("UserPromptSubmit"),
    };
    let is_start = hook == J::str("SessionStart");
    let is_prompt = hook == J::str("UserPromptSubmit");
    // Python `title.split(SEP, 1)` on a str; a list or dict title only fails if it contains SEP.
    let title = match event.get("session_title") {
        Some(v) if v.truthy() => v.clone(),
        _ => J::str(""),
    };
    let current = match &title {
        J::Str(s) => s.split_once(SEP).map_or("", |(head, _)| py_strip(head)),
        J::Arr(items) if !items.contains(&J::str(SEP)) => "",
        J::Obj(items) if !items.iter().any(|(k, _)| k == SEP) => "",
        _ => return Err(Abort),
    };
    // SessionStart carries no title: a fresh session may reuse an app row already
    // named by an earlier session, so leave naming to the first UserPromptSubmit.
    let (name, told) = claim(&session, current, now(), !is_start)?;
    let Some(name) = name else {
        return Ok(None);
    };
    let br = branch::branch(&text_field(event, "cwd", ".")?)?;
    let new = if br.is_empty() { name.clone() } else { format!("{name}{SEP}{br}") };
    let mut out = vec![("hookEventName".to_owned(), hook)];
    if is_prompt && title != J::Str(new.clone()) {
        out.push(("sessionTitle".into(), J::str(&new)));
    }
    let mut context = None;
    // SessionStart fires on startup, resume, /clear and compaction: each may drop what the agent knew.
    if is_start || told != Some(J::str(&name)) {
        context = Some(format!("Ton prénom dans cette session : {name} (titre « {new} »)."));
    }
    // The desktop app ignores sessionTitle and auto-titles the first turn; only its MCP tool can fix it.
    if is_prompt && told.is_none() && std::env::var_os("CLAUDE_CODE_ENTRYPOINT").is_some_and(|v| v == "claude-desktop") {
        if let Some(c) = context.as_mut() {
            c.push_str(&format!(
                " Renomme la session sans bloquer le tour : charge mcp__ccd_session_mgmt__set_session_title \
                 via ToolSearch dans le même message que tes premiers appels d'outils, puis appelle-le \
                 avec session_id=\"self\", title=\"{new}\" en parallèle des suivants (sans get_session)."
            ));
        }
    }
    if let Some(c) = context {
        out.push(("additionalContext".into(), J::Str(c)));
    }
    Ok((out.len() > 1).then(|| J::Obj(vec![("hookSpecificOutput".into(), J::Obj(out))])))
}

fn run() -> Result<Option<J>, Abort> {
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).map_err(|_| Abort)?;
    let event: J = serde_json::from_slice(&input).map_err(|_| Abort)?;
    answer(&event)
}

fn main() {
    if let Ok(Some(result)) = run() {
        let _ = writeln!(std::io::stdout(), "{}", result.dumps(false));
    }
}
