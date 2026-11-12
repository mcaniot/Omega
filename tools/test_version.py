"""Version consistency: rust/Cargo.toml is the single source, CHANGELOG.md and `omega --version` follow it."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('OMEGA_OPTIMIZE_BINARY', ROOT / 'rust/target/release/optimize'))


def cargo_version():
    match = re.search(r'^version = "([^"]+)"$', (ROOT / 'rust/Cargo.toml').read_text(), re.M)
    assert match, 'rust/Cargo.toml has no package version'
    return match.group(1)


def test_version_is_semver():
    assert re.fullmatch(r'\d+\.\d+\.\d+', cargo_version())


def test_changelog_latest_release_matches_cargo():
    releases = re.findall(r'^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$', (ROOT / 'CHANGELOG.md').read_text(), re.M)
    assert releases and releases[0] == cargo_version()


def test_cargo_lock_matches_cargo():
    lock = (ROOT / 'rust/Cargo.lock').read_text()
    assert f'name = "omega-tools"\nversion = "{cargo_version()}"' in lock


def test_omega_prints_version():
    for arg in ('--version', 'version'):
        out = subprocess.run([str(BINARY), arg], capture_output=True, text=True, check=True).stdout
        assert out == f'omega {cargo_version()}\n'
