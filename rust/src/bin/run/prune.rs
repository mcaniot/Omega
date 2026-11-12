//! Dry-run (or `--apply`) removal of old evidence not referenced by active or completed tasks.

use crate::argparse::Parsed;
use crate::py;
use regex::Regex;
use std::collections::HashSet;
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

const NAME: &str = r"\d{8}T\d{6}-(?:native-[A-Za-z0-9]{6}|[\w-]{8})";

fn io(path: &Path) -> impl Fn(std::io::Error) -> String + '_ {
    move |e| py::oserror(e.raw_os_error().unwrap_or(0), path.as_os_str().as_bytes())
}

pub fn prune(p: &Parsed, home: &[u8]) -> Result<Vec<u8>, String> {
    let days = p.float("days").unwrap_or(14.0);
    if days <= 0.0 {
        return Err("--days must be positive".into());
    }
    let apply = p.flag("apply");
    let root = Path::new(OsStr::from_bytes(home)).join(".agents/tasks/evidence");
    let find = Regex::new(NAME).unwrap();
    let exact = Regex::new(&format!("^(?:{})$", NAME)).unwrap();
    let mut referenced: HashSet<String> = HashSet::new();
    // Completed tasks keep their cited evidence too: it is small and stays verifiable.
    for tasks in ["active", "completed"].map(|d| root.parent().unwrap().join(d)) {
        let Ok(entries) = std::fs::read_dir(&tasks) else { continue };
        for entry in entries {
            let path = entry.map_err(io(&tasks))?.path();
            if path.as_os_str().as_bytes().ends_with(b".md") {
                let text = std::fs::read(&path).map_err(io(&path))?;
                for m in find.find_iter(&String::from_utf8_lossy(&text)) {
                    referenced.insert(m.as_str().to_string());
                }
            }
        }
    }
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64();
    let cutoff = now - days * 86400.0;
    let (mut candidates, mut size) = (Vec::new(), 0u64);
    for entry in std::fs::read_dir(&root).map_err(io(&root))? {
        let path = entry.map_err(io(&root))?.path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let link = std::fs::symlink_metadata(&path).map_err(io(&path))?;
        let Ok(meta) = std::fs::metadata(&path) else { continue };
        if !meta.is_dir() || link.file_type().is_symlink() || referenced.contains(&name) || !exact.is_match(&name) {
            continue;
        }
        if (meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9) < cutoff {
            for file in std::fs::read_dir(&path).map_err(io(&path))? {
                let file = file.map_err(io(&path))?.path();
                if let Ok(m) = std::fs::metadata(&file) {
                    if m.is_file() {
                        size += m.len();
                    }
                }
            }
            candidates.push(path);
        }
    }
    if apply {
        for path in &candidates {
            std::fs::remove_dir_all(path).map_err(io(path))?;
        }
    }
    Ok(format!(
        "{{\"status\": \"PASS\", \"applied\": {}, \"runs\": {}, \"bytes\": {}, \"kept_referenced\": {}}}",
        apply,
        candidates.len(),
        size,
        referenced.len()
    )
    .into_bytes())
}
