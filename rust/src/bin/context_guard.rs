//! Bound Claude Read calls without granting native permissions or changing media reads.
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::path::Path;

const LARGE_TEXT_BYTES: u64 = 8192;
const MAX_READ_LINES: u64 = 200;

fn deny(reason: &str) -> Value {
    json!({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})
}

fn evaluate(payload: &Value) -> Value {
    if payload.get("tool_name").and_then(Value::as_str) != Some("Read") {
        return json!({});
    }
    let input = &payload["tool_input"];
    let Some(file) = input.get("file_path").and_then(Value::as_str) else {
        return json!({}); // Let Read report its own argument errors.
    };
    let path = Path::new(file);
    let Ok(metadata) = path.metadata() else {
        return json!({}); // Preserve native access checks and missing-file errors.
    };
    if !metadata.is_file() || metadata.len() <= LARGE_TEXT_BYTES {
        return json!({});
    }
    let extension = path
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if matches!(
        extension.as_str(),
        "png"
            | "jpg"
            | "jpeg"
            | "gif"
            | "webp"
            | "bmp"
            | "tif"
            | "tiff"
            | "ico"
            | "avif"
            | "pdf"
            | "ipynb"
    ) {
        return json!({}); // Serialized image bytes are not text tokens.
    }
    if let (Some(home), Ok(real)) = (std::env::var_os("HOME"), path.canonicalize()) {
        let evidence = Path::new(&home).join(".agents/tasks/evidence");
        let evidence = evidence.canonicalize().unwrap_or(evidence);
        if real.starts_with(evidence) {
            return deny(
                "Large evidence file: use Ω-inspect ARTIFACT with --search or --offset/--lines; do not Read the full log.",
            );
        }
    }
    match input.get("limit").and_then(Value::as_u64) {
        Some(1..=MAX_READ_LINES) => json!({}),
        _ => deny(
            "Large text file: locate the symbol, then Read with limit 1..200 and a targeted offset. Keep logs in evidence and inspect bounded slices.",
        ),
    }
}

fn main() {
    let mut input = Vec::new();
    let output = match std::io::stdin().read_to_end(&mut input) {
        Ok(_) => match serde_json::from_slice::<Value>(&input) {
            Ok(payload) => evaluate(&payload),
            Err(_) => deny("Invalid context guard JSON."),
        },
        Err(_) => deny("Cannot read context guard input."),
    };
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{output}");
}
